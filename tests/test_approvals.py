"""阶段 6-A：审批数据模型 + 服务层。

用临时 SQLite 库（ensure_schema 已经建好 approvals 表），不碰 Redis、不碰 HTTP。
模拟人类决策 = 直接调服务函数，和 HTTP 层解耦。
"""

import pytest

from app.approvals import (
    ApprovalAlreadyDecidedError,
    ApprovalNotFoundError,
    create_approval_request,
    decide_approval,
    list_pending_approvals,
)
from app.db import connect
from app.state_machine import InvalidTransitionError, transition
from app.workflow_model import TASK_STATUSES
from app.workflow_store import SQLiteWorkflowStore


def make_db(tmp_path):
    """建好 schema 的临时库，返回 db_path。"""
    db_path = str(tmp_path / "approvals.db")
    SQLiteWorkflowStore(db_path)          # 构造函数里会 ensure_schema
    return db_path


def seed_task(db_path, task_id="task-1", status="WAITING_APPROVAL", question="任务：写报告"):
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, question, status, max_iterations) VALUES (?, ?, ?, 5)",
            (task_id, question, status),
        )
        conn.commit()
    finally:
        conn.close()


def seed_workflow(db_path, workflow_id="wf-1", status="RUNNING"):
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO workflows (id, goal, status, dag) VALUES (?, '目标', ?, '{\"nodes\": []}')",
            (workflow_id, status),
        )
        conn.commit()
    finally:
        conn.close()


def read_one(db_path, sql, params):
    conn = connect(db_path, row_factory=True)
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def read_approval(db_path, approval_id):
    return read_one(db_path, "SELECT * FROM approvals WHERE id = ?", (approval_id,))


def read_task(db_path, task_id="task-1"):
    return read_one(db_path, "SELECT * FROM tasks WHERE id = ?", (task_id,))


def read_workflow(db_path, workflow_id="wf-1"):
    return read_one(db_path, "SELECT * FROM workflows WHERE id = ?", (workflow_id,))


# ---------------------------------------------------------------------------
# A1：状态机 + 状态集合
# ---------------------------------------------------------------------------

def test_waiting_approval_is_a_known_task_status():
    assert "WAITING_APPROVAL" in TASK_STATUSES


def test_state_machine_allows_approval_edges():
    # 批准：等待 -> 待执行（还没开始跑，所以回 PENDING 而不是 RUNNING）
    assert transition({"status": "WAITING_APPROVAL"}, "PENDING")["status"] == "PENDING"
    # 拒绝：等待 -> 取消
    assert transition({"status": "WAITING_APPROVAL"}, "CANCELLED")["status"] == "CANCELLED"
    # 动态审批（6-D）：执行到一半停下来等人
    assert transition({"status": "RUNNING"}, "WAITING_APPROVAL")["status"] == "WAITING_APPROVAL"
    # 等待中的任务不能直接跳到完成
    with pytest.raises(InvalidTransitionError):
        transition({"status": "WAITING_APPROVAL"}, "COMPLETED")


# ---------------------------------------------------------------------------
# A2：待审批列表
# ---------------------------------------------------------------------------

def test_list_pending_approvals_returns_reason_and_question(tmp_path):
    db_path = make_db(tmp_path)
    seed_task(db_path, question="任务：对外发布这份报告")
    approval_id = create_approval_request(
        db_path, "task-1", workflow_id="wf-1", node_id="publish",
        reason="这一步会把报告发到公网，需要人工确认",
    )

    pending = list_pending_approvals(db_path)

    assert len(pending) == 1
    item = pending[0]
    assert item["id"] == approval_id
    assert item["task_id"] == "task-1"
    assert item["node_id"] == "publish"
    assert item["reason"] == "这一步会把报告发到公网，需要人工确认"
    assert item["question"] == "任务：对外发布这份报告"      # JOIN tasks 拿到的原文


def test_list_pending_approvals_skips_decided_ones(tmp_path):
    db_path = make_db(tmp_path)
    seed_task(db_path)
    create_approval_request(db_path, "task-1", workflow_id="wf-1", node_id="publish", reason="r")

    decide_approval(db_path, read_pending_id(db_path), approved=True, comment="同意")

    assert list_pending_approvals(db_path) == []


def read_pending_id(db_path):
    conn = connect(db_path)
    try:
        return conn.execute("SELECT id FROM approvals LIMIT 1").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# A3：决策（本阶段核心）
# ---------------------------------------------------------------------------

def test_decide_approval_approves_task_and_asks_http_layer_to_enqueue(tmp_path):
    db_path = make_db(tmp_path)
    seed_task(db_path)
    approval_id = create_approval_request(
        db_path, "task-1", workflow_id="wf-1", node_id="publish", reason="需要确认"
    )

    result = decide_approval(
        db_path, approval_id, approved=True, comment="批准，但金额改成 100", decided_by="老板"
    )

    assert result["should_enqueue"] is True
    assert result["task_status"] == "PENDING"

    approval = read_approval(db_path, approval_id)
    assert approval["status"] == "APPROVED"
    assert approval["comment"] == "批准，但金额改成 100"
    assert approval["decided_by"] == "老板"
    assert approval["decided_at"] is not None

    task = read_task(db_path)
    assert task["status"] == "PENDING"
    # 人类意见必须进入 Agent 的上下文，否则 Agent 不知道被审批过
    assert "【人工审批】" in task["question"]
    assert "金额改成 100" in task["question"]


def test_decide_approval_rejects_task_and_fails_workflow(tmp_path):
    db_path = make_db(tmp_path)
    seed_task(db_path)
    seed_workflow(db_path)
    approval_id = create_approval_request(
        db_path, "task-1", workflow_id="wf-1", node_id="publish", reason="需要确认"
    )

    result = decide_approval(db_path, approval_id, approved=False, comment="风险太大，不发")

    assert result["should_enqueue"] is False
    assert read_approval(db_path, approval_id)["status"] == "REJECTED"
    assert read_task(db_path)["status"] == "CANCELLED"
    # 只断言"理由被记录下来了"，不规定文案格式（文案是实现的自由，语义才该被规定）
    assert "风险太大，不发" in read_task(db_path)["answer"]
    # 人类否决是确定性事件：workflow 直接失败，不走节点级重试
    assert read_workflow(db_path)["status"] == "FAILED"


def test_decide_approval_twice_raises(tmp_path):
    db_path = make_db(tmp_path)
    seed_task(db_path)
    approval_id = create_approval_request(db_path, "task-1", workflow_id="wf-1", node_id="p", reason="r")
    decide_approval(db_path, approval_id, approved=True)

    with pytest.raises(ApprovalAlreadyDecidedError):
        decide_approval(db_path, approval_id, approved=False)


def test_decide_unknown_approval_raises(tmp_path):
    db_path = make_db(tmp_path)

    with pytest.raises(ApprovalNotFoundError):
        decide_approval(db_path, "不存在的审批 id", approved=True)


def test_approval_without_comment_marks_question(tmp_path):
    """不带意见批准时，question 里仍然要有【人工审批】标记（学生补的 A4）。"""
    db_path = make_db(tmp_path)
    seed_task(db_path)
    approval_id = create_approval_request(db_path, "task-1", workflow_id="wf-1", node_id="p", reason="r")
    decide_approval(db_path, approval_id, approved=True)

    task = read_task(db_path)
    assert task["status"] == "PENDING"
    assert "【人工审批】已批准。" in task["question"]
