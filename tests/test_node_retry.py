"""阶段 5-G：节点级失败重试。

要验证的完整链路：
    节点任务 FAILED → 调度器重派（新建一条 task 行）→ 还没到上限就继续
    → 到了上限才 workflow FAILED

这里刻意用**真实的 SQLiteWorkflowStore**（临时库），不用假 store：
"最新一次尝试说了算" 和 "attempts 怎么数" 正是本次要实现的逻辑，
用假对象测就等于把答案先写进测试里了。

模拟 worker 的执行结果 = 直接 UPDATE tasks 的状态（见 finish()）。
"""

import uuid

from app.db import connect
from app.dag import DAG
from app.scheduler import dispatch_ready
from app.workflow_runner import COMPLETED, DISPATCHED, FAILED, WorkflowScheduler
from app.workflow_store import SQLiteWorkflowStore


def diamond_dag():
    """A → (B, C) → D"""
    dag = DAG()
    dag.add_node("A", {"description": "收集资料"})
    dag.add_node("B", {"description": "分析市场", "depends_on": ["A"]})
    dag.add_node("C", {"description": "分析政策", "depends_on": ["A"]})
    dag.add_node("D", {"description": "写报告", "depends_on": ["B", "C"]})
    dag.add_edge("A", "B")
    dag.add_edge("A", "C")
    dag.add_edge("B", "D")
    dag.add_edge("C", "D")
    return dag


def make_store(tmp_path):
    return SQLiteWorkflowStore(str(tmp_path / "node_retry.db"))


def new_workflow(store):
    return store.create_workflow("目标", {"nodes": []}, status="RUNNING")


def finish(db_path, task_id, status, answer=None):
    """模拟 worker 跑完一次尝试：把 task 行写成 COMPLETED / FAILED。"""
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET status = ?, answer = ? WHERE id = ?",
            (status, answer, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def make_scheduler(store, **kwargs):
    """返回 (调度器, 已入队的 task_id 列表)。"""
    enqueued: list[str] = []
    scheduler = WorkflowScheduler(
        store, diamond_dag(), enqueue=enqueued.append, poll_interval=0, **kwargs
    )
    return scheduler, enqueued


# ---------------------------------------------------------------------------
# G1：存储层 —— attempts 计数 + "最新一次尝试说了算"
# ---------------------------------------------------------------------------

def test_scheduling_state_counts_attempts_and_uses_latest_attempt(tmp_path):
    """第一次 FAILED、第二次 COMPLETED：节点算完成，不算失败。"""
    store = make_store(tmp_path)
    workflow_id = new_workflow(store)

    first = store.create_node_task(workflow_id, "A", "第一次", [])
    finish(store.db_path, first, "FAILED", "第一次挂了")
    second = store.create_node_task(workflow_id, "A", "第二次", [])
    finish(store.db_path, second, "COMPLETED", "第二次成功")

    state = store.get_scheduling_state(workflow_id)

    assert state["attempts"]["A"] == 2
    assert "A" in state["completed"]
    assert "A" not in state["failed"]
    assert state["outputs"]["A"] == "第二次成功"


def test_scheduling_state_keeps_node_failed_when_all_attempts_failed(tmp_path):
    """两次都 FAILED：节点算失败，attempts 也要数对。"""
    store = make_store(tmp_path)
    workflow_id = new_workflow(store)

    for round_number in (1, 2):
        task_id = store.create_node_task(workflow_id, "A", f"第 {round_number} 次", [])
        finish(store.db_path, task_id, "FAILED", f"第 {round_number} 次挂了")

    state = store.get_scheduling_state(workflow_id)

    assert state["attempts"]["A"] == 2
    assert "A" in state["failed"]
    assert "A" not in state["completed"]
    assert "A" not in state["outputs"]


# ---------------------------------------------------------------------------
# G2：纯决策 —— retryable 允许重派，但不能绕过依赖检查
# ---------------------------------------------------------------------------

def test_dispatch_ready_redispatches_retryable_node():
    dag = diamond_dag()

    assert set(dispatch_ready(dag, {"A"}, {"B"}, retryable={"B"})) == {"B", "C"}


def test_dispatch_ready_does_not_redispatch_without_retryable():
    """不在 retryable 里的已派发节点仍然不能重派（防重复烧钱的老规矩）。"""
    dag = diamond_dag()

    assert dispatch_ready(dag, {"A"}, {"B"}) == ["C"]


def test_dispatch_ready_keeps_dependency_check_for_retryable_node():
    """retryable 只放宽"派发过也允许重派"，不能放宽依赖检查。"""
    dag = diamond_dag()

    # D 依赖 B、C，而它们都还没完成；D 即使在 retryable 里也不能被派发
    assert dispatch_ready(dag, {"A"}, {"B", "C", "D"}, retryable={"D"}) == []


# ---------------------------------------------------------------------------
# G3：主循环 —— 重派、耗尽后才失败、重试成功不算失败
# ---------------------------------------------------------------------------

def test_tick_redispatches_failed_node_and_keeps_workflow_running(tmp_path):
    store = make_store(tmp_path)
    workflow_id = new_workflow(store)
    scheduler, enqueued = make_scheduler(store)

    assert scheduler.tick(workflow_id) == DISPATCHED
    finish(store.db_path, enqueued[0], "FAILED", "第一次挂了")

    assert scheduler.tick(workflow_id) == DISPATCHED
    assert store.get_workflow(workflow_id)["status"] == "RUNNING"
    assert len(enqueued) == 2                       # 重派 = 入队一条新任务
    tasks = store.list_node_tasks(workflow_id)
    assert [task["node_id"] for task in tasks] == ["A", "A"]  # 两次尝试都留档


def test_tick_fails_workflow_after_attempts_exhausted(tmp_path):
    store = make_store(tmp_path)
    workflow_id = new_workflow(store)
    scheduler, enqueued = make_scheduler(store, max_node_attempts=2)

    scheduler.tick(workflow_id)
    finish(store.db_path, enqueued[0], "FAILED", "第一次挂了")

    assert scheduler.tick(workflow_id) == DISPATCHED       # 还有一次机会
    finish(store.db_path, enqueued[1], "FAILED", "第二次也挂了")

    assert scheduler.tick(workflow_id) == FAILED
    assert store.get_workflow(workflow_id)["status"] == "FAILED"


def test_node_failed_then_succeeded_does_not_fail_workflow(tmp_path):
    """重试成功过的节点：旧的 FAILED 行还在表里，但 workflow 必须能走到 COMPLETED。"""
    store = make_store(tmp_path)
    workflow_id = new_workflow(store)
    scheduler, enqueued = make_scheduler(store)

    scheduler.tick(workflow_id)
    finish(store.db_path, enqueued[0], "FAILED", "第一次挂了")
    assert scheduler.tick(workflow_id) == DISPATCHED  # 重派 A
    assert len(enqueued) == 2                        # 第二次尝试已经入队
    finish(store.db_path, enqueued[1], "COMPLETED", "重试成功")

    assert scheduler.tick(workflow_id) == DISPATCHED  # A 完成后派发 B、C
    for task in store.list_node_tasks(workflow_id):
        if task["node_id"] in ("B", "C"):
            finish(store.db_path, task["id"], "COMPLETED", f"{task['node_id']} 完成")

    assert scheduler.tick(workflow_id) == DISPATCHED  # 派发 D
    for task in store.list_node_tasks(workflow_id):
        if task["node_id"] == "D":
            finish(store.db_path, task["id"], "COMPLETED", "最终报告")

    assert scheduler.tick(workflow_id) == COMPLETED
    assert store.get_workflow(workflow_id)["status"] == COMPLETED


def test_failed_node_with_downstream_dependency_cannot_dispatch_downstream(tmp_path):
    """节点还在重试期间，它的下游绝不能被提前派发。"""
    store = make_store(tmp_path)
    workflow_id = new_workflow(store)
    scheduler, enqueued = make_scheduler(store, max_node_attempts=2)

    # 第一次派发 A
    assert scheduler.tick(workflow_id) == DISPATCHED
    finish(store.db_path, enqueued[0], "FAILED", "第一次挂了")

    # 第二次派发 A，A 还没完成，B、C 不应该被派发
    assert scheduler.tick(workflow_id) == DISPATCHED
    assert len(enqueued) == 2  # 第二次尝试已经入队
    assert all(task["node_id"] == "A" for task in store.list_node_tasks(workflow_id))

    # 完成 A 后，B、C 才能被派发
    finish(store.db_path, enqueued[1], "COMPLETED", "重试成功")
    assert scheduler.tick(workflow_id) == DISPATCHED
    dispatched_nodes = {task["node_id"] for task in store.list_node_tasks(workflow_id)}
    assert dispatched_nodes == {"A", "B", "C"}
