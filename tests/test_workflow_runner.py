"""Workflow 调度主循环测试。

关键手法：用内存版 FakeStore 顶替 SQLiteWorkflowStore。
调度器只依赖 store 的 4 个方法，所以换个假实现就能离线测主循环，
不需要数据库、不需要 Redis、不需要真实 LLM。
"""

import pytest

from app.dag import DAG
from app.workflow_runner import (
    COMPLETED,
    DISPATCHED,
    FAILED,
    WAITING,
    WorkflowScheduler,
    collect_final_output,
    sink_nodes,
)


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


class FakeStore:
    """内存版存储层：接口和 SQLiteWorkflowStore 一致，但不落盘。"""

    def __init__(self):
        self.workflow = {"id": "wf-1", "status": "RUNNING", "final_output": None}
        self.tasks: list[dict] = []
        self.auto_complete = False
        self._next_id = 0

    def get_scheduling_state(self, workflow_id):
        """契约和 SQLiteWorkflowStore 完全一致：节点状态由"最新一次尝试"说了算。

        self.tasks 是追加顺序（第 1 次尝试在前、重试在后），所以顺序遍历、
        后来者覆盖前者，最后留下的就是这个节点最新一次尝试的状态。
        假对象一旦和真实现语义不一致，就会变成"测试全绿但线上出错"的温床。
        """
        completed, failed, dispatched, outputs, attempts = set(), set(), set(), {}, {}
        fatal: set[str] = set()
        for task in self.tasks:
            node_id = task["node_id"]
            if not node_id:
                continue
            dispatched.add(node_id)
            attempts[node_id] = attempts.get(node_id, 0) + 1
            if task["status"] == "COMPLETED":
                completed.add(node_id)
                failed.discard(node_id)
                fatal.discard(node_id)
                if task["answer"]:
                    outputs[node_id] = task["answer"]
            elif task["status"] == "FAILED":
                failed.add(node_id)
                completed.discard(node_id)
                fatal.discard(node_id)
                outputs.pop(node_id, None)
            elif task["status"] == "BUDGET_EXCEEDED":
                # 阶段 7-A-2：确定性失败，单独成 fatal —— 混进 failed 会被当成"还有额度"
                fatal.add(node_id)
                completed.discard(node_id)
                failed.discard(node_id)
                outputs.pop(node_id, None)
            else:
                completed.discard(node_id)
                failed.discard(node_id)
                fatal.discard(node_id)
                outputs.pop(node_id, None)
        return {
            "completed": completed,
            "failed": failed,
            "fatal": fatal,
            "dispatched": dispatched,
            "outputs": outputs,
            "attempts": attempts,
        }

    def create_node_task(
        self, workflow_id, node_id, question, depends_on,
        upstream_outputs=None, max_iterations=5, status="PENDING",
    ):
        # status 参数是阶段 6-B 加的：需要审批的节点"一出生就在等人"（WAITING_APPROVAL）
        self._next_id += 1
        task = {
            "id": f"task-{self._next_id}",
            "node_id": node_id,
            "question": question,
            "depends_on": list(depends_on),
            "upstream_outputs": dict(upstream_outputs or {}),
            "status": status,
            "answer": None,
        }
        self.tasks.append(task)
        if self.auto_complete:
            task["status"] = "COMPLETED"
            task["answer"] = f"{node_id} 的答案"
        return task["id"]

    def update_workflow_status(self, workflow_id, status, final_output=None):
        self.workflow["status"] = status
        if final_output is not None:
            self.workflow["final_output"] = final_output

    # ---------- 测试辅助 ----------
    def task_for(self, node_id):
        matches = [t for t in self.tasks if t["node_id"] == node_id]
        assert matches, f"节点 {node_id} 还没有被派发"
        return matches[-1]

    def complete(self, node_id, answer):
        task = self.task_for(node_id)
        task["status"] = "COMPLETED"
        task["answer"] = answer

    def fail(self, node_id):
        self.task_for(node_id)["status"] = "FAILED"


def make_scheduler(store, **kwargs):
    """返回 (调度器, 已入队的 task_id 列表)。"""
    enqueued: list[str] = []
    scheduler = WorkflowScheduler(
        store, diamond_dag(), enqueue=enqueued.append, poll_interval=0, **kwargs
    )
    return scheduler, enqueued


def test_tick_exposes_dispatched_nodes_for_event_publishing():
    """tick 要把本轮派发的节点暴露出来，编排器才能发 node_dispatched 事件。"""
    store = FakeStore()
    scheduler, _ = make_scheduler(store)

    assert scheduler.tick("wf-1") == DISPATCHED
    assert scheduler.last_dispatched == ["A"]

    store.complete("A", "资料")
    assert scheduler.tick("wf-1") == DISPATCHED
    assert set(scheduler.last_dispatched) == {"B", "C"}

    assert scheduler.tick("wf-1") == WAITING
    assert scheduler.last_dispatched == []          # 没派发就清空，别让上一轮结果被误读


def test_tick_exposes_redispatched_node_and_clears_on_terminal():
    """重派也算法派发；进入终态后必须清空。"""
    store = FakeStore()
    scheduler, _ = make_scheduler(store, max_node_attempts=2)

    scheduler.tick("wf-1")
    store.fail("A")
    assert scheduler.tick("wf-1") == DISPATCHED
    assert scheduler.last_dispatched == ["A"]       # 节点级重试也算一次派发

    store.fail("A")                                  # 重试额度用光
    assert scheduler.tick("wf-1") == FAILED
    assert scheduler.last_dispatched == []


def test_sink_nodes_and_final_output():
    dag = diamond_dag()
    assert sink_nodes(dag) == ["D"]
    assert collect_final_output(dag, {"A": "资料", "D": "最终报告"}) == "最终报告"


def test_tick_dispatches_roots_only():
    store = FakeStore()
    scheduler, enqueued = make_scheduler(store)
    assert scheduler.tick("wf-1") == DISPATCHED
    assert [t["node_id"] for t in store.tasks] == ["A"]
    assert enqueued == ["task-1"]
    assert store.task_for("A")["status"] == "PENDING"


def test_tick_does_not_redispatch_running_nodes():
    """A 已派发但还没跑完时，第二轮不能再建一条 A 的任务。"""
    store = FakeStore()
    scheduler, enqueued = make_scheduler(store)
    scheduler.tick("wf-1")
    assert scheduler.tick("wf-1") == WAITING
    assert len(store.tasks) == 1
    assert enqueued == ["task-1"]


def test_tick_dispatches_downstream_with_upstream_outputs():
    store = FakeStore()
    scheduler, _ = make_scheduler(store)
    scheduler.tick("wf-1")
    store.complete("A", "资料")
    assert scheduler.tick("wf-1") == DISPATCHED
    assert {t["node_id"] for t in store.tasks} == {"A", "B", "C"}
    for node_id in ("B", "C"):
        task = store.task_for(node_id)
        assert task["upstream_outputs"] == {"A": "资料"}
        assert "资料" in task["question"]


def test_tick_completes_workflow_with_final_output():
    store = FakeStore()
    scheduler, _ = make_scheduler(store)
    scheduler.tick("wf-1")
    store.complete("A", "资料")
    scheduler.tick("wf-1")
    store.complete("B", "市场结论")
    store.complete("C", "政策结论")
    scheduler.tick("wf-1")
    store.complete("D", "最终报告")
    assert scheduler.tick("wf-1") == COMPLETED
    assert store.workflow["status"] == COMPLETED
    assert store.workflow["final_output"] == "最终报告"


def test_tick_fails_workflow_only_after_retries_exhausted():
    """5-G 新行为：单次失败先重派；重试次数真的用光，workflow 才 FAILED。"""
    store = FakeStore()
    scheduler, _ = make_scheduler(store, max_node_attempts=2)
    scheduler.tick("wf-1")
    store.fail("A")
    assert scheduler.tick("wf-1") == DISPATCHED     # 还有一次机会：重派 A
    assert store.workflow["status"] == "RUNNING"    # 不能提前判死
    store.fail("A")                                  # 第 2 次尝试又失败
    assert scheduler.tick("wf-1") == FAILED
    assert store.workflow["status"] == FAILED


def test_tick_fails_immediately_when_retries_disabled():
    """max_node_attempts=1 等于关掉重试，退回"一次失败就 FAILED"（旧行为的显式表达）。

    顺带守住边界条件：判定必须是 attempts < max_node_attempts。
    如果写成 <=，这条测试会立刻发现——节点会被无限重派，永远等不到 FAILED。
    """
    store = FakeStore()
    scheduler, _ = make_scheduler(store, max_node_attempts=1)
    scheduler.tick("wf-1")
    store.fail("A")
    assert scheduler.tick("wf-1") == FAILED
    assert store.workflow["status"] == FAILED


def test_run_until_terminal_stops_when_completed():
    store = FakeStore()
    store.auto_complete = True  # 派发即完成，模拟 worker 很快
    scheduler, _ = make_scheduler(store)
    assert scheduler.run_until_terminal("wf-1") == COMPLETED
    assert store.workflow["status"] == COMPLETED
    assert store.workflow["final_output"] == "D 的答案"


def test_run_until_terminal_respects_max_ticks():
    store = FakeStore()  # 永远不完成，模拟卡死
    scheduler, _ = make_scheduler(store, max_ticks=3)
    with pytest.raises(RuntimeError, match="max_ticks"):
        scheduler.run_until_terminal("wf-1")


# TODO T3（学生任务）：自己再加 1 个测试。
# 提示方向：① 菱形汇聚：B、C 都完成前 D 不能被派发；
#          ② 有节点 FAILED 时不应再派发新节点（先判终态，不做无用功）；
#          ③ max_ticks=None 时不设上限（别让这个分支永远测不到）。
def test_tick_does_not_dispatch_downstream_if_upstream_not_completed():
    store = FakeStore()
    scheduler, _ = make_scheduler(store)
    scheduler.tick("wf-1")
    store.complete("A", "资料")
    scheduler.tick("wf-1")
    # 此时 B、C 已派发，但 D 还不能派发，因为 B、C 都没完成
    assert {t["node_id"] for t in store.tasks} == {"A", "B", "C"}
    assert scheduler.tick("wf-1") == WAITING
