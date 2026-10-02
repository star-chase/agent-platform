"""回归测试：Agent 反复拿不到新信息时必须强制收尾，而不是耗尽迭代被判 FAILED。

对应 2026-09-17 真实联调发现的"调研类节点原地打转"问题——
阶段 1 任务 1-F / 思考题 7 的遗留项，终于有了真实复现：
调研节点连续 5 轮调用 search，拿到的都是同一句 mock 结果，最后 max_iterations 耗尽 → FAILED。
"""

import json

from app.agent_fc_persistent import AgentFCPersistentExecutor
from app.progress_guard import NO_PROGRESS_HINT
from tests.test_parallel_tool_calls import (
    FakeClient,
    FakeMessage,
    FakeResponse,
    FakeToolCall,
    FINAL_MESSAGE,
    PARALLEL_MESSAGE,
    make_db,
    read_steps,
)


REPEATED_MESSAGE = FakeMessage(
    tool_calls=[FakeToolCall("call-1", "search", json.dumps({"query": "北京人口"}))]
)
FORCED_ANSWER_MESSAGE = FakeMessage(content="根据已有信息：北京人口约 2100 万。")


class ToolAwareCompletions:
    """模拟真实协议：带 tools 时模型又想调工具；不带 tools 时只能出文本。"""

    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("tools"):
            return FakeResponse(REPEATED_MESSAGE)
        return FakeResponse(FORCED_ANSWER_MESSAGE)


class ToolAwareClient:
    def __init__(self):
        self.completions = ToolAwareCompletions()
        self.chat = type("Chat", (), {"completions": self.completions})()


def make_stuck_executor(tmp_path, max_iterations=5):
    db_path = make_db(tmp_path)
    client = ToolAwareClient()
    executor = AgentFCPersistentExecutor(
        llm_client=client, model="fake-model", max_iterations=max_iterations, db_path=db_path
    )
    return executor, client, db_path


def test_repeated_tool_results_force_a_final_answer(tmp_path):
    """一直重复同样结果的 Agent 必须被强制收尾，任务仍是 COMPLETED。"""
    executor, client, _db_path = make_stuck_executor(tmp_path)
    result = executor.run("查一下北京人口")

    assert result["success"] is True
    assert result.get("forced_final_answer") is True
    assert result["answer"] == "根据已有信息：北京人口约 2100 万。"
    # 强制收尾的那一次调用不能再带 tools，否则模型还会继续调工具
    assert "tools" not in client.completions.calls[-1]


def test_no_progress_hint_is_injected_before_forcing(tmp_path):
    """第一次检测到无进展时，应该先注入提示催促收尾。"""
    executor, client, _db_path = make_stuck_executor(tmp_path)
    executor.run("查一下北京人口")

    all_messages = [m for call in client.completions.calls for m in call["messages"]]
    hints = [
        m for m in all_messages
        if isinstance(m, dict)
        and m.get("role") == "user"
        and NO_PROGRESS_HINT[:12] in str(m.get("content"))
    ]
    assert hints, "应该先注入 NO_PROGRESS_HINT 再强制收尾"


def test_forced_answer_path_writes_no_duplicate_iterations(tmp_path):
    """强制收尾也要遵守 UNIQUE(task_id, iteration)：不能再来一次崩溃。"""
    executor, _client, db_path = make_stuck_executor(tmp_path)
    result = executor.run("查一下北京人口")

    steps = read_steps(db_path, result["task_id"])
    iterations = [step["iteration"] for step in steps]
    assert len(iterations) == len(set(iterations))
    assert steps[-1]["type"] == "final_answer"


def test_no_progress_detection_does_not_fire_on_progress(tmp_path):
    """有正常进展时不能被误触发（一轮工具调用 → 一轮最终答案）。"""
    db_path = make_db(tmp_path)
    client = FakeClient([PARALLEL_MESSAGE, FINAL_MESSAGE])
    executor = AgentFCPersistentExecutor(
        llm_client=client, model="fake-model", max_iterations=5, db_path=db_path
    )
    result = executor.run("对比北京和上海的人口")

    assert result["success"] is True
    assert "forced_final_answer" not in result


# ---------------------------------------------------------------------------
# 2026-09-17 真实验收又发现的两个洞（当前红）
#   洞 1：resume() 里那份"重复的 Agent 循环"没接无进展检测，续跑仍会原地打转；
#   洞 2：模型返回"空消息"（既无 tool_calls 也无 content）时什么都不做，
#         白烧迭代次数，极端情况下最后被判"达到最大迭代次数"——而模型其实
#         并没有在调工具，只是这一轮没说话。
# 根因都是同一个：run() 和 resume() 各有一份复制粘贴的循环，改一处漏一处。
# ---------------------------------------------------------------------------

from tests.test_parallel_tool_calls import seed_failed_parallel_task  # noqa: E402


def test_resume_also_forces_final_answer_when_stuck(tmp_path):
    """续跑时反复拿不到新信息，也必须强制收尾，而不是耗尽迭代。"""
    db_path = make_db(tmp_path)
    seed_failed_parallel_task(db_path)

    client = ToolAwareClient()
    executor = AgentFCPersistentExecutor(
        llm_client=client, model="fake-model", max_iterations=6, db_path=db_path
    )
    result = executor.resume("task-1")

    assert result["success"] is True
    assert result.get("forced_final_answer") is True
    steps = read_steps(db_path, "task-1")
    iterations = [step["iteration"] for step in steps]
    assert len(iterations) == len(set(iterations))     # 仍然不能撞 UNIQUE
    assert steps[-1]["type"] == "final_answer"


EMPTY_MESSAGE = FakeMessage()  # 既没有 tool_calls，也没有 content


class EmptyThenAnswerCompletions:
    """带 tools 时模型一直"沉默"；不带 tools 时才肯出答案。"""

    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("tools"):
            return FakeResponse(EMPTY_MESSAGE)
        return FakeResponse(FORCED_ANSWER_MESSAGE)


class EmptyThenAnswerClient:
    def __init__(self):
        self.completions = EmptyThenAnswerCompletions()
        self.chat = type("Chat", (), {"completions": self.completions})()


def test_empty_responses_do_not_burn_the_whole_iteration_budget(tmp_path):
    """空响应不能白烧迭代：连续几轮没内容也要强制收尾。"""
    db_path = make_db(tmp_path)
    client = EmptyThenAnswerClient()
    executor = AgentFCPersistentExecutor(
        llm_client=client, model="fake-model", max_iterations=5, db_path=db_path
    )
    result = executor.run("查一下北京人口")

    assert result["success"] is True
    assert result.get("forced_final_answer") is True
    assert len(client.completions.calls) < 5   # 不能把 5 轮全烧光
