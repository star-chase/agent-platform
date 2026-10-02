"""阶段 6-C：审批决策服务（决策 + 重新入队）。"""

import pytest

from app.approval_submit import decide_and_enqueue
from app.approvals import create_approval_request
from app.db import connect
from app.workflow_store import SQLiteWorkflowStore


def make_db(tmp_path):
    db_path = str(tmp_path / "submit.db")
    SQLiteWorkflowStore(db_path)
    return db_path


def seed_task(db_path, task_id="task-1", status="WAITING_APPROVAL"):
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, question, status, max_iterations) VALUES (?, '任务：发布报告', ?, 5)",
            (task_id, status),
        )
        conn.commit()
    finally:
        conn.close()


def test_decide_and_enqueue_enqueues_approved_task(tmp_path):
    db_path = make_db(tmp_path)
    seed_task(db_path)
    approval_id = create_approval_request(db_path, "task-1", workflow_id="wf-1", node_id="publish", reason="要发公网")
    enqueued: list[str] = []

    result = decide_and_enqueue(
        approval_id, approved=True, comment="同意", db_path=db_path, enqueue=enqueued.append
    )

    assert result["should_enqueue"] is True
    assert enqueued == ["task-1"]          # 批准后由这里投递，worker 才拿得到


def test_decide_and_enqueue_skips_enqueue_on_rejection(tmp_path):
    db_path = make_db(tmp_path)
    seed_task(db_path)
    approval_id = create_approval_request(db_path, "task-1", workflow_id="wf-1", node_id="publish", reason="要发公网")
    enqueued: list[str] = []

    result = decide_and_enqueue(
        approval_id, approved=False, comment="不行", db_path=db_path, enqueue=enqueued.append
    )

    assert result["should_enqueue"] is False
    assert enqueued == []                  # 被拒绝的任务绝不能进队列


def test_decide_and_enqueue_fails_when_enqueue_is_none(tmp_path):
    """批准了却没注入入队函数：必须报错（fail closed），不能静默跳过。"""
    db_path = make_db(tmp_path)
    seed_task(db_path)
    approval_id = create_approval_request(db_path, "task-1", workflow_id="wf-1", node_id="publish", reason="要发公网")

    with pytest.raises(RuntimeError) as exc_info:
        decide_and_enqueue(
            approval_id, approved=True, comment="同意", db_path=db_path, enqueue=None
        )

    assert "部署配置漏了" in str(exc_info.value)
