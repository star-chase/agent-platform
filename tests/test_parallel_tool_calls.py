"""回归测试：一次 LLM 响应里有多个并行工具调用时，一轮只能写一条 step。

背景（2026-09-17 真实联调炸出来的 bug）：
DeepSeek 在调研类任务里会一次返回 2-6 个 search 调用，而
`AgentFCPersistentExecutor.run()` 在 `for tool_call in message.tool_calls` 循环里
每次都调 `_save_step(iteration=iteration + 1)`，第 2 条就撞
`UNIQUE(task_id, iteration)` → worker 进程崩溃 → 任务永远卡在 RUNNING。

为什么 95 个测试全绿还是没发现？因为**所有假 LLM 都只返回 1 个 tool_call**。
这个文件补上缺失的那条路径：假 LLM 一次返回 2 个工具调用。
"""

import json

from app.agent_fc_persistent import AgentFCPersistentExecutor
from app.db import connect, ensure_schema


class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    def __init__(self, tool_calls=None, content=None):
        self.tool_calls = tool_calls
        self.content = content

    def model_dump(self):
        """真实 OpenAI message 对象有 model_dump()，假对象必须一起模仿。

        少这个方法时 _save_step 会直接 TypeError，说明替身没模拟到位。
        """
        return {
            "role": "assistant",
            "content": self.content,
            "tool_calls": [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                for tool_call in (self.tool_calls or [])
            ],
        }


class FakeResponse:
    def __init__(self, message, input_tokens=100, output_tokens=20):
        self.choices = [type("Choice", (), {"message": message})()]
        # 阶段 7-A：真实响应带 usage；假对象必须一起模仿，
        # 否则"token 计量"这条路径在测试里永远走不到。
        self.usage = type("Usage", (), {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
        })()


class FakeCompletions:
    def __init__(self, messages):
        self._messages = list(messages)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._messages:
            raise AssertionError("假 LLM 的响应已用尽：执行器调 LLM 的次数比预期多")
        return FakeResponse(self._messages.pop(0))


class FakeClient:
    def __init__(self, messages):
        self.completions = FakeCompletions(messages)
        self.chat = type("Chat", (), {"completions": self.completions})()


# 第 1 轮：一次返回两个工具调用（真实 DeepSeek 就是这么干的）
PARALLEL_MESSAGE = FakeMessage(
    tool_calls=[
        FakeToolCall("call-1", "search", json.dumps({"query": "北京"})),
        FakeToolCall("call-2", "search", json.dumps({"query": "上海"})),
    ]
)
# 第 2 轮：给出最终答案
FINAL_MESSAGE = FakeMessage(content="北京约 2100 万，上海约 2500 万。")


def make_executor(tmp_path, messages):
    """返回 (执行器, 假客户端, 临时库路径)。"""
    db_path = str(tmp_path / "test.db")
    conn = connect(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    client = FakeClient(messages)
    executor = AgentFCPersistentExecutor(
        llm_client=client, model="fake-model", max_iterations=3, db_path=db_path
    )
    return executor, client, db_path


def read_steps(db_path, task_id):
    conn = connect(db_path, row_factory=True)
    try:
        rows = conn.execute(
            "SELECT iteration, type, tool_name, tool_args, observation FROM steps "
            "WHERE task_id = ? ORDER BY iteration",
            (task_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def test_parallel_tool_calls_write_one_step_per_iteration(tmp_path):
    """一轮两个工具调用，只应写一条 step；任务最终 COMPLETED。"""
    executor, _client, db_path = make_executor(tmp_path, [PARALLEL_MESSAGE, FINAL_MESSAGE])
    result = executor.run("对比北京和上海的人口")

    assert result["success"] is True
    steps = read_steps(db_path, result["task_id"])
    assert [step["iteration"] for step in steps] == [1, 2]
    assert steps[0]["type"] == "tool_call"
    assert steps[1]["type"] == "final_answer"


def test_parallel_tool_results_are_all_sent_back(tmp_path):
    """协议要求：每个 tool_call 都必须有配对的 tool 消息，一个都不能漏。"""
    executor, client, _db_path = make_executor(tmp_path, [PARALLEL_MESSAGE, FINAL_MESSAGE])
    executor.run("对比北京和上海的人口")

    second_messages = client.completions.calls[1]["messages"]
    tool_messages = [
        m for m in second_messages if isinstance(m, dict) and m.get("role") == "tool"
    ]
    assert len(tool_messages) == 2
    assert {m["tool_call_id"] for m in tool_messages} == {"call-1", "call-2"}


def test_parallel_tool_observations_are_not_lost(tmp_path):
    """合并成一条 step 时，两个工具的结果都不能丢。"""
    executor, _client, db_path = make_executor(tmp_path, [PARALLEL_MESSAGE, FINAL_MESSAGE])
    result = executor.run("对比北京和上海的人口")

    step = read_steps(db_path, result["task_id"])[0]
    assert "北京" in step["observation"]
    assert "上海" in step["observation"]


# ---------------------------------------------------------------------------
# resume() 的配套回归：重建 messages 时必须把 tool_calls 和 observation 一一配对
# ---------------------------------------------------------------------------

ASSISTANT_MESSAGE = {
    "role": "assistant",
    "content": None,
    "tool_calls": [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "search", "arguments": json.dumps({"query": "北京"})},
        },
        {
            "id": "call-2",
            "type": "function",
            "function": {"name": "search", "arguments": json.dumps({"query": "上海"})},
        },
    ],
}


def make_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = connect(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    return db_path


def seed_failed_parallel_task(db_path, task_id="task-1"):
    """造一条"跑了一半、含并行工具调用、状态 FAILED"的任务。"""
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, question, status, max_iterations) "
            "VALUES (?, '对比北京和上海的人口', 'FAILED', 5)",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO steps (id, task_id, iteration, type, tool_name, tool_args, "
            "observation, llm_message_json) VALUES "
            "('step-1', ?, 1, 'tool_call', ?, ?, ?, ?)",
            (
                task_id,
                json.dumps(["search", "search"], ensure_ascii=False),
                json.dumps([{"query": "北京"}, {"query": "上海"}], ensure_ascii=False),
                json.dumps(["北京约 2100 万", "上海约 2500 万"], ensure_ascii=False),
                json.dumps(ASSISTANT_MESSAGE, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_resume_rebuilds_one_tool_message_per_call(tmp_path):
    """resume 重建历史时，每个 tool_call 都要有配对的 tool 消息。"""
    db_path = make_db(tmp_path)
    seed_failed_parallel_task(db_path)

    client = FakeClient([FINAL_MESSAGE])
    executor = AgentFCPersistentExecutor(
        llm_client=client, model="fake-model", max_iterations=5, db_path=db_path
    )
    result = executor.resume("task-1")

    assert result["success"] is True
    sent_messages = client.completions.calls[0]["messages"]
    tool_messages = [
        m for m in sent_messages if isinstance(m, dict) and m.get("role") == "tool"
    ]
    assert [m["tool_call_id"] for m in tool_messages] == ["call-1", "call-2"]
    assert tool_messages[0]["content"] == "北京约 2100 万"
    assert tool_messages[1]["content"] == "上海约 2500 万"


def test_resume_writes_one_step_per_iteration_with_parallel_calls(tmp_path):
    """resume 之后继续跑，遇到并行工具调用也不能再撞 UNIQUE。"""
    db_path = make_db(tmp_path)
    seed_failed_parallel_task(db_path)

    client = FakeClient([PARALLEL_MESSAGE, FINAL_MESSAGE])
    executor = AgentFCPersistentExecutor(
        llm_client=client, model="fake-model", max_iterations=5, db_path=db_path
    )
    result = executor.resume("task-1")

    assert result["success"] is True
    steps = read_steps(db_path, "task-1")
    # 第 1 步是种进去的，第 2 步是 resume 后并行调用的，第 3 步是最终答案
    assert [step["iteration"] for step in steps] == [1, 2, 3]
    assert steps[2]["type"] == "final_answer"
