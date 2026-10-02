"""编排进程测试（阶段 5-D-4）。

这里刻意**不用假 store**，而是用真实的 SQLiteWorkflowStore + 临时库：
- 单元测试用假对象（`test_workflow_runner.py` 的 FakeStore）：快、隔离、测单个逻辑；
- 集成测试用真对象 + 临时库（本文件）：测"store 的 SQL 和编排器的假设是否对得上"。
两种都要有，缺一不可——只写假对象会在"假对象和真实现在细节上不一致"时漏 bug。
"""

import sqlite3

from app.workflow_runner import COMPLETED, DISPATCHED, FAILED
from app.workflow_store import SQLiteWorkflowStore
from app.orchestrator import WorkflowOrchestrator


DIAMOND_DAG = {
    "nodes": [
        {"id": "A", "description": "收集资料", "agent_type": "researcher", "depends_on": []},
        {"id": "B", "description": "分析市场", "agent_type": "analyst", "depends_on": ["A"]},
        {"id": "C", "description": "分析政策", "agent_type": "analyst", "depends_on": ["A"]},
        {"id": "D", "description": "写报告", "agent_type": "writer", "depends_on": ["B", "C"]},
    ]
}


def make_orchestrator(tmp_path, publish=None, request_approval=None):
    """返回 (store, orchestrator, enqueued, db_path)。"""
    db_path = str(tmp_path / "test.db")
    store = SQLiteWorkflowStore(db_path)
    enqueued: list[str] = []
    orchestrator = WorkflowOrchestrator(
        store, enqueue=enqueued.append, poll_interval=0, publish=publish,
        request_approval=request_approval,
    )
    return store, orchestrator, enqueued, db_path


def set_node_status(db_path, workflow_id, node_id, status, answer=None):
    """测试辅助：模拟 worker 执行完后把某个节点的任务改成 COMPLETED / FAILED。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET status = ?, answer = ? WHERE workflow_id = ? AND node_id = ?",
            (status, answer, workflow_id, node_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_run_once_dispatches_roots_of_running_workflow(tmp_path):
    store, orchestrator, enqueued, _ = make_orchestrator(tmp_path)
    workflow_id = store.create_workflow("目标", DIAMOND_DAG, status="RUNNING")

    assert orchestrator.run_once() == [(workflow_id, DISPATCHED)]
    tasks = store.list_node_tasks(workflow_id)
    assert [t["node_id"] for t in tasks] == ["A"]
    assert enqueued == [tasks[0]["id"]]


def test_run_once_publishes_node_dispatched_events(tmp_path):
    """派发节点时要发 node_dispatched，否则前端在节点跑完前一直没动静。"""
    published: list[tuple[str, dict]] = []
    store, orchestrator, _, _ = make_orchestrator(
        tmp_path, publish=lambda workflow_id, event: published.append((workflow_id, event))
    )
    workflow_id = store.create_workflow("目标", DIAMOND_DAG, status="RUNNING")

    orchestrator.run_once()

    assert published == [(
        workflow_id,
        {"type": "node_dispatched", "node_id": "A", "status": "PENDING"},
    )]

    # 跑完整条链路，看事件序列是否符合"两层进度"：每个节点先 dispatched、最后 workflow_finished
    set_node_status(store.db_path, workflow_id, "A", "COMPLETED", "资料")
    orchestrator.run_once()                                  # 派发 B、C
    set_node_status(store.db_path, workflow_id, "B", "COMPLETED", "市场")
    set_node_status(store.db_path, workflow_id, "C", "COMPLETED", "政策")
    orchestrator.run_once()                                  # 派发 D
    set_node_status(store.db_path, workflow_id, "D", "COMPLETED", "报告")
    orchestrator.run_once()                                  # 终态

    assert [event["type"] for _, event in published] == [
        "node_dispatched",      # A
        "node_dispatched",      # B
        "node_dispatched",      # C
        "node_dispatched",      # D
        "workflow_finished",
    ]
    assert published[-1][1]["status"] == COMPLETED


APPROVAL_DAG = {
    "nodes": [
        {"id": "publish", "description": "对外发布报告", "agent_type": "writer",
         "requires_approval": True, "depends_on": []},
    ]
}


def test_run_once_publishes_waiting_approval_event(tmp_path):
    """需要审批的节点：不发 node_dispatched，改发 node_waiting_approval。

    否则一个"等人类批"的 workflow 在前端看起来和"卡死"没区别。
    """
    published: list[dict] = []
    approvals: list[str] = []
    store, orchestrator, enqueued, db_path = make_orchestrator(
        tmp_path,
        publish=lambda workflow_id, event: published.append(event),
        request_approval=lambda workflow_id, node_id, task_id, reason: approvals.append(node_id),
    )
    workflow_id = store.create_workflow("目标", APPROVAL_DAG, status="RUNNING")

    orchestrator.run_once()

    assert approvals == ["publish"]                              # 建了审批记录
    assert enqueued == []                                        # 绝不能入队
    assert [event["type"] for event in published] == ["node_waiting_approval"]
    assert published[0]["node_id"] == "publish"
    assert published[0]["status"] == "WAITING_APPROVAL"
    assert store.list_node_tasks(workflow_id)[0]["status"] == "WAITING_APPROVAL"


def test_run_once_ignores_non_running_workflows(tmp_path):
    store, orchestrator, enqueued, _ = make_orchestrator(tmp_path)
    workflow_id = store.create_workflow("目标", DIAMOND_DAG, status="PLANNING")

    assert orchestrator.run_once() == []
    assert store.list_node_tasks(workflow_id) == []
    assert enqueued == []


def test_full_lifecycle_marks_workflow_completed(tmp_path):
    """A → (B, C) → D 走完整生命周期，最终 workflow COMPLETED 且 final_output 正确。"""
    store, orchestrator, _, db_path = make_orchestrator(tmp_path)
    workflow_id = store.create_workflow("目标", DIAMOND_DAG, status="RUNNING")

    orchestrator.run_once()                                   # 派发 A
    set_node_status(db_path, workflow_id, "A", "COMPLETED", "资料")
    orchestrator.run_once()                                   # 派发 B、C
    set_node_status(db_path, workflow_id, "B", "COMPLETED", "市场结论")
    set_node_status(db_path, workflow_id, "C", "COMPLETED", "政策结论")
    orchestrator.run_once()                                   # 派发 D
    set_node_status(db_path, workflow_id, "D", "COMPLETED", "最终报告")

    assert orchestrator.run_once() == [(workflow_id, COMPLETED)]
    workflow = store.get_workflow(workflow_id)
    assert workflow["status"] == COMPLETED
    assert workflow["final_output"] == "最终报告"


def test_run_once_retries_failed_node_then_marks_workflow_failed(tmp_path):
    """5-G：节点失败先重派（workflow 保持 RUNNING）；3 次机会用光后才 FAILED。"""
    store, orchestrator, _, db_path = make_orchestrator(tmp_path)
    workflow_id = store.create_workflow("目标", DIAMOND_DAG, status="RUNNING")

    orchestrator.run_once()                                     # 派发 A（第 1 次尝试）
    set_node_status(db_path, workflow_id, "A", "FAILED")

    assert orchestrator.run_once() == [(workflow_id, DISPATCHED)]   # 重派 = 第 2 次尝试
    assert store.get_workflow(workflow_id)["status"] == "RUNNING"   # 还有额度，不能判死
    set_node_status(db_path, workflow_id, "A", "FAILED")

    assert orchestrator.run_once() == [(workflow_id, DISPATCHED)]   # 重派 = 第 3 次尝试
    set_node_status(db_path, workflow_id, "A", "FAILED")

    assert len(store.list_node_tasks(workflow_id)) == 3          # 三次尝试全部留档
    assert orchestrator.run_once() == [(workflow_id, FAILED)]    # 额度用光 → FAILED
    assert store.get_workflow(workflow_id)["status"] == FAILED


def test_run_once_advances_only_running_workflows(tmp_path):
    store, orchestrator, _, db_path = make_orchestrator(tmp_path)
    workflow_id1 = store.create_workflow("目标1", DIAMOND_DAG, status="RUNNING")
    workflow_id2 = store.create_workflow("目标2", DIAMOND_DAG, status="PLANNING")
    workflow_id3 = store.create_workflow("目标3", DIAMOND_DAG, status="RUNNING")

    result = orchestrator.run_once()
    assert len(result) == 2
    assert all(status == DISPATCHED for _, status in result)
    assert {workflow_id for workflow_id, _ in result} == {workflow_id1, workflow_id3}
