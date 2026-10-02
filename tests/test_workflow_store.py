"""Workflow 存储层测试。

用 pytest 运行：python -m pytest tests/test_workflow_store.py -q
tmp_path 是 pytest 内置 fixture：每个测试拿到一个独立的临时目录，测试之间互不污染。
"""

import sqlite3

import pytest

from app.workflow_store import SQLiteWorkflowStore, ensure_workflow_schema


SAMPLE_DAG = {
    "nodes": [
        {"id": "collect", "description": "收集资料"},
        {"id": "write", "description": "写报告", "depends_on": ["collect"]},
    ]
}


def make_store(tmp_path) -> SQLiteWorkflowStore:
    return SQLiteWorkflowStore(str(tmp_path / "test.db"))


def set_task_status(db_path: str, task_id: str, status: str, answer: str | None = None) -> None:
    """测试辅助：模拟 worker 执行完后更新任务状态。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET status = ?, answer = ? WHERE id = ?",
            (status, answer, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_schema_contains_workflow_columns(tmp_path):
    db_path = str(tmp_path / "test.db")
    store = SQLiteWorkflowStore(db_path)  # __init__ 里会建表
    conn = sqlite3.connect(db_path)
    try:
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        workflow_columns = {row[1] for row in conn.execute("PRAGMA table_info(workflows)")}
    finally:
        conn.close()
    assert {"workflow_id", "node_id", "depends_on", "upstream_outputs"} <= task_columns
    assert {"id", "goal", "status", "dag", "final_output"} <= workflow_columns


def test_schema_migration_is_idempotent(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    try:
        ensure_workflow_schema(conn)
        ensure_workflow_schema(conn)  # 再跑一次不应报错
    finally:
        conn.close()
    assert make_store(tmp_path).get_workflow("nope") is None


def test_create_and_get_workflow_roundtrip(tmp_path):
    store = make_store(tmp_path)
    workflow_id = store.create_workflow("调研新能源汽车并写报告", SAMPLE_DAG, status="RUNNING")
    workflow = store.get_workflow(workflow_id)
    assert workflow["goal"] == "调研新能源汽车并写报告"   # 中文没被转义
    assert workflow["status"] == "RUNNING"
    assert workflow["dag"] == SAMPLE_DAG                # dag 被还原成 dict
    assert workflow["final_output"] is None


def test_get_missing_workflow_returns_none(tmp_path):
    assert make_store(tmp_path).get_workflow("does-not-exist") is None


def test_update_workflow_status_persists_final_output(tmp_path):
    store = make_store(tmp_path)
    workflow_id = store.create_workflow("目标", SAMPLE_DAG)
    store.update_workflow_status(workflow_id, "COMPLETED", final_output="最终报告")
    workflow = store.get_workflow(workflow_id)
    assert workflow["status"] == "COMPLETED"
    assert workflow["final_output"] == "最终报告"


def test_create_node_task_persists_workflow_fields(tmp_path):
    store = make_store(tmp_path)
    workflow_id = store.create_workflow("目标", SAMPLE_DAG)
    task_id = store.create_node_task(
        workflow_id,
        node_id="write",
        question="任务：写报告\n\n上游任务结果：\n【collect】\n资料",
        depends_on=["collect"],
        upstream_outputs={"collect": "资料"},
    )
    tasks = store.list_node_tasks(workflow_id)
    assert len(tasks) == 1
    task = tasks[0]
    assert task["id"] == task_id
    assert task["node_id"] == "write"
    assert task["status"] == "PENDING"
    assert task["depends_on"] == ["collect"]
    assert task["upstream_outputs"] == {"collect": "资料"}


def test_list_node_tasks_ignores_legacy_tasks(tmp_path):
    """阶段 3 的老任务没有 workflow_id，不应混进 workflow 的子任务列表。"""
    store = make_store(tmp_path)
    workflow_id = store.create_workflow("目标", SAMPLE_DAG)
    store.create_node_task(workflow_id, "collect", "任务：收集资料", [])

    conn = sqlite3.connect(str(tmp_path / "test.db"))
    try:
        conn.execute(
            "INSERT INTO tasks (id, question, status) VALUES ('legacy-1', '老任务', 'COMPLETED')"
        )
        conn.commit()
    finally:
        conn.close()

    tasks = store.list_node_tasks(workflow_id)
    assert [task["node_id"] for task in tasks] == ["collect"]


def test_scheduling_state_aggregates_all_categories(tmp_path):
    store = make_store(tmp_path)
    db_path = str(tmp_path / "test.db")
    workflow_id = store.create_workflow("目标", SAMPLE_DAG)

    done_id = store.create_node_task(workflow_id, "A", "任务：A", [])
    failed_id = store.create_node_task(workflow_id, "B", "任务：B", [])
    store.create_node_task(workflow_id, "C", "任务：C", [])
    set_task_status(db_path, done_id, "COMPLETED", answer="A 的结果")
    set_task_status(db_path, failed_id, "FAILED")

    state = store.get_scheduling_state(workflow_id)
    assert state["completed"] == {"A"}
    assert state["failed"] == {"B"}
    assert state["dispatched"] == {"A", "B", "C"}
    assert state["outputs"] == {"A": "A 的结果"}


def test_scheduling_state_is_empty_for_new_workflow(tmp_path):
    store = make_store(tmp_path)
    workflow_id = store.create_workflow("目标", SAMPLE_DAG)
    state = store.get_scheduling_state(workflow_id)
    assert state == {
        "completed": set(),
        "failed": set(),
        # 阶段 7-A-2 加了 fatal（预算超支这类确定性失败）：返回结构变了，
        # 这条"整字典相等"的测试必须跟着改 —— 契约变更是要主动付账的。
        "fatal": set(),
        "dispatched": set(),
        "outputs": {},
        "attempts": {},
    }


def test_list_workflow_ids_by_status(tmp_path):
    store = make_store(tmp_path)
    running_id = store.create_workflow("运行中", SAMPLE_DAG, status="RUNNING")
    store.create_workflow("规划中", SAMPLE_DAG, status="PLANNING")
    assert store.list_workflow_ids_by_status("RUNNING") == [running_id]
    assert store.list_workflow_ids_by_status("COMPLETED") == []


def test_create_workflow_with_invalid_status_raises_value_error(tmp_path):
    store = make_store(tmp_path)
    # pytest.raises 的两个好处：
    #   1) 没抛异常时测试会自己失败，不用手写 try/except/else + assert False；
    #   2) match 只匹配异常文案里的稳定片段，不绑死整句中文（文案改了测试也不会假红）。
    with pytest.raises(ValueError, match="INVALID_STATUS"):
        store.create_workflow("目标", SAMPLE_DAG, status="INVALID_STATUS")
