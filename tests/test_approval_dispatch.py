"""阶段 6-B：调度器的审批门控（静态审批点）。

用 test_workflow_runner 的内存版 FakeStore + 一个"记录审批请求"的假回调。
要验证的核心：需要审批的节点**一出生就在等人**（WAITING_APPROVAL），
绝不入队；而且缺审批处理器时必须报错，不能静默放行。
"""

import pytest

from app.dag import DAG
from app.workflow_runner import DISPATCHED, WAITING, WorkflowScheduler
from app.workflow_store import SQLiteWorkflowStore
from tests.test_workflow_runner import FakeStore


def approval_dag():
    """A（需要人工审批）-> B"""
    dag = DAG()
    dag.add_node("A", {"description": "对外发布报告", "requires_approval": True})
    dag.add_node("B", {"description": "归档", "depends_on": ["A"]})
    dag.add_edge("A", "B")
    return dag


def plain_dag():
    dag = DAG()
    dag.add_node("A", {"description": "收集资料"})
    return dag


def make_scheduler(store, dag, **kwargs):
    """返回 (调度器, 已入队列表, 审批请求列表)。"""
    enqueued: list[str] = []
    approvals: list[tuple] = []
    scheduler = WorkflowScheduler(
        store, dag, enqueue=enqueued.append, poll_interval=0,
        request_approval=lambda workflow_id, node_id, task_id, reason: approvals.append(
            (workflow_id, node_id, task_id, reason)
        ),
        **kwargs,
    )
    return scheduler, enqueued, approvals


# ---------------------------------------------------------------------------
# B1：建行时能指定初始状态
# ---------------------------------------------------------------------------

def test_create_node_task_honours_status(tmp_path):
    store = SQLiteWorkflowStore(str(tmp_path / "dispatch.db"))
    workflow_id = store.create_workflow("目标", {"nodes": []}, status="RUNNING")

    task_id = store.create_node_task(
        workflow_id, "A", "任务：发布报告", [], status="WAITING_APPROVAL"
    )

    tasks = store.list_node_tasks(workflow_id)
    assert tasks[0]["id"] == task_id
    assert tasks[0]["status"] == "WAITING_APPROVAL"
    # 白名单式入口：非法状态必须当场拒绝
    with pytest.raises(ValueError):
        store.create_node_task(workflow_id, "B", "任务：B", [], status="NOT_A_STATUS")


# ---------------------------------------------------------------------------
# B2：审批门控
# ---------------------------------------------------------------------------

def test_approval_gated_node_is_parked_instead_of_enqueued():
    store = FakeStore()
    scheduler, enqueued, approvals = make_scheduler(store, approval_dag())

    assert scheduler.tick("wf-1") == DISPATCHED

    task = store.task_for("A")
    assert task["status"] == "WAITING_APPROVAL"      # 一出生就在等人
    assert enqueued == []                             # 绝不能入队
    assert len(approvals) == 1
    workflow_id, node_id, task_id, reason = approvals[0]
    assert (workflow_id, node_id, task_id) == ("wf-1", "A", task["id"])
    assert reason == "对外发布报告"                    # 给人类看"这步要干什么"
    assert scheduler.last_waiting_approval == ["A"]


def test_normal_node_is_still_enqueued():
    """守卫测试：没标 requires_approval 的节点，行为一点都不能变。"""
    store = FakeStore()
    scheduler, enqueued, approvals = make_scheduler(store, plain_dag())

    assert scheduler.tick("wf-1") == DISPATCHED

    assert enqueued == [store.task_for("A")["id"]]
    assert approvals == []
    assert scheduler.last_waiting_approval == []


def test_parked_node_blocks_downstream():
    """等审批的节点没完成，下游不能跑；也不能被重复派发。"""
    store = FakeStore()
    scheduler, enqueued, _ = make_scheduler(store, approval_dag())

    scheduler.tick("wf-1")
    assert scheduler.tick("wf-1") == WAITING
    assert [t["node_id"] for t in store.tasks] == ["A"]   # 只有 A，且没有第二条
    assert enqueued == []


def test_missing_approval_handler_fails_closed():
    """需要审批却没注入处理器：必须报错，绝不能静默放行。

    审批属于安全边界，语义是 fail closed —— 宁可整个调度失败，
    也不能让一个"本该等人批准"的节点偷偷跑起来。
    """
    store = FakeStore()
    scheduler = WorkflowScheduler(store, approval_dag(), enqueue=[].append, poll_interval=0)

    with pytest.raises(RuntimeError):
        scheduler.tick("wf-1")


def test_parked_node_is_not_a_failure_and_downstream_waits_for_completion():
    """挂着等审批的节点：既不能被当失败重派，也不能让下游提前跑。

    两个容易搞错的点（这就是这条测试的价值）：
      1) "批准"只是把 task 改回 PENDING 并**由审批 API 重新入队**，
         调度器不负责入队，也不会因为"批准了"就派发下游；
      2) 下游能不能跑，取决于上游**完成**（不是"通过了"）——
         所以这里要模拟 worker 真正把 A 跑完。
    """
    store = FakeStore()
    scheduler, enqueued, approvals = make_scheduler(store, approval_dag())

    # 第一次 tick：A 挂起等审批
    assert scheduler.tick("wf-1") == DISPATCHED
    assert scheduler.last_waiting_approval == ["A"]
    assert enqueued == []
    assert len(approvals) == 1

    # 挂起 ≠ 失败：再 tick 什么都不该发生（不能被当失败重派）
    assert scheduler.tick("wf-1") == WAITING
    assert [t["node_id"] for t in store.tasks] == ["A"]
    assert enqueued == []

    # 人类批准 + worker 最终把 A 跑完（批准入队那部分属于 approval_submit 的测试范围）
    store.complete("A", "报告已发布")

    # 这时下游才允许派发，而且只有 B 会被入队（A 从来没进过队列）
    assert scheduler.tick("wf-1") == DISPATCHED
    assert set(t["node_id"] for t in store.tasks) == {"A", "B"}
    assert enqueued == [store.task_for("B")["id"]]