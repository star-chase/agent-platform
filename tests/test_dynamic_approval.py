"""阶段 6-D：动态审批 —— Agent 自己请求人类批准 -> 暂停 -> 批准后带着人类意见续跑。

这是阶段 3 的 resume 能力真正发光的地方：
暂停时**不留悬空的 tool_call**（否则重建 messages 时 API 会报"每个 tool_call 必须有
tool_result"），恢复时把人类的决定当作一条 user 消息注入上下文。
"""

import json

from app.agent_fc_persistent import AgentFCPersistentExecutor
from app.approvals import create_approval_request, decide_approval
from app.db import connect, ensure_schema
from tests.test_parallel_tool_calls import (
    FINAL_MESSAGE,
    FakeClient,
    FakeMessage,
    FakeToolCall,
)


APPROVAL_REQUEST = FakeMessage(
    tool_calls=[
        FakeToolCall(
            "call-approve-1",
            "request_human_approval",
            json.dumps({"reason": "要把报告发到公网", "proposed_action": "调用发布接口"}),
        )
    ]
)


def make_executor(tmp_path, messages, client=None):
    """建执行器；request_approval 用**真的** approvals 服务写库（不是假回调）。"""
    db_path = str(tmp_path / "dynamic.db")
    conn = connect(db_path)
    ensure_schema(conn)
    conn.close()
    client = client or FakeClient(messages)
    executor = AgentFCPersistentExecutor(
        llm_client=client, model="fake-model", max_iterations=3, db_path=db_path,
        request_approval=lambda task_id, reason: create_approval_request(
            db_path, task_id, "wf-1", "publish", reason
        ),
    )
    return executor, client, db_path


def read_task(db_path, task_id):
    conn = connect(db_path, row_factory=True)
    try:
        return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
    finally:
        conn.close()


def read_steps(db_path, task_id):
    conn = connect(db_path, row_factory=True)
    try:
        rows = conn.execute(
            "SELECT iteration, type, tool_name, tool_args, observation FROM steps "
            "WHERE task_id = ? ORDER BY iteration", (task_id,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def read_approval(db_path):
    conn = connect(db_path, row_factory=True)
    try:
        return dict(conn.execute("SELECT * FROM approvals").fetchone())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# D1：暂停
# ---------------------------------------------------------------------------

def test_agent_pauses_and_creates_approval_request(tmp_path):
    executor, client, db_path = make_executor(tmp_path, [APPROVAL_REQUEST])

    result = executor.run("把报告发到公网")

    # 不是成功，但也绝不是失败 —— worker 必须能区分这两种情况
    assert result["status"] == "WAITING_APPROVAL"
    assert result["success"] is False
    assert result["approval_id"]

    # 暂停之后不再继续问 LLM（假客户端只给了一条响应，多问一次就会 AssertionError）
    assert len(client.completions.calls) == 1

    task = read_task(db_path, result["task_id"])
    assert task["status"] == "WAITING_APPROVAL"

    steps = read_steps(db_path, result["task_id"])
    assert [s["type"] for s in steps] == ["approval_request"]
    assert steps[0]["tool_name"] == "request_human_approval"

    approval = read_approval(db_path)
    assert approval["task_id"] == result["task_id"]
    # 给人类看的信息要全：为什么 + 打算做什么
    assert "要把报告发到公网" in approval["reason"]
    assert "调用发布接口" in approval["reason"]


# ---------------------------------------------------------------------------
# D2：续跑（注入人类决定）
# ---------------------------------------------------------------------------

def test_resume_injects_human_decision_and_finishes(tmp_path):
    executor, _client, db_path = make_executor(tmp_path, [APPROVAL_REQUEST])
    paused = executor.run("把报告发到公网")
    task_id = paused["task_id"]

    decide_approval(
        db_path, paused["approval_id"], approved=True, comment="同意，但结尾要加数据来源"
    )

    executor2, client2, _ = make_executor(tmp_path, [FINAL_MESSAGE])
    result = executor2.resume(task_id)

    assert result["success"] is True
    contents = " ".join(
        message.get("content") or "" for message in client2.completions.calls[0]["messages"]
    )
    assert "【人工审批】已批准" in contents          # 决定进了上下文
    assert "数据来源" in contents                    # 人类的具体要求也在
    assert read_task(db_path, task_id)["status"] == "COMPLETED"


def test_resume_puts_task_back_to_running(tmp_path):
    """续跑期间任务必须是 RUNNING —— 否则进程崩了，recovery 找不到这个僵尸任务。

    技巧：让假客户端在"被调用的那一刻"读一下数据库，就能测到这种瞬时状态。
    """
    executor, _client, db_path = make_executor(tmp_path, [APPROVAL_REQUEST])
    paused = executor.run("把报告发到公网")
    task_id = paused["task_id"]
    decide_approval(db_path, paused["approval_id"], approved=True)

    client2 = FakeClient([FINAL_MESSAGE])
    seen_statuses = []
    original_create = client2.completions.create

    def sniffing_create(**kwargs):
        seen_statuses.append(read_task(db_path, task_id)["status"])
        return original_create(**kwargs)

    client2.completions.create = sniffing_create
    executor2, _, _ = make_executor(tmp_path, None, client=client2)
    executor2.resume(task_id)

    assert seen_statuses == ["RUNNING"]


def test_resume_fails_if_approval_rejected(tmp_path):
    """被拒绝的任务不该再被续跑：resume 直接返回"已取消"（学生补的 D3）。"""
    executor, _client, db_path = make_executor(tmp_path, [APPROVAL_REQUEST])
    paused = executor.run("把报告发到公网")
    task_id = paused["task_id"]
    decide_approval(db_path, paused["approval_id"], approved=False)

    executor2, client2, _ = make_executor(tmp_path, [FINAL_MESSAGE])
    result = executor2.resume(task_id)

    assert result["success"] is False
    assert result["status"] == "CANCELLED"