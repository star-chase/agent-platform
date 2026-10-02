from app.dag import CycleDetectedError
from app.planner import PlannerError, extract_plan_from_tool_call, plan_to_dag


def test_valid_plan_becomes_dag():
    dag = plan_to_dag({
        "nodes": [
            {"id": "collect", "description": "收集资料"},
            {"id": "write", "description": "写报告", "depends_on": ["collect"]},
        ]
    })
    assert dag.ready_nodes(set()) == ["collect"]
    assert dag.ready_nodes({"collect"}) == ["write"]


def test_cycle_in_plan_is_rejected():
    try:
        plan_to_dag({
            "nodes": [
                {"id": "A", "depends_on": ["B"]},
                {"id": "B", "depends_on": ["A"]},
            ]
        })
    except PlannerError:
        pass
    else:
        raise AssertionError("Planner 生成的循环计划应该被拒绝")


def test_plan_node_limit_is_enforced():
    try:
        plan_to_dag({"nodes": [{"id": str(i)} for i in range(3)]}, max_nodes=2)
    except PlannerError:
        pass
    else:
        raise AssertionError("超过节点上限的计划应该被拒绝")


def test_unknown_dependency_is_rejected():
    try:
        plan_to_dag({"nodes": [{"id": "write", "depends_on": ["missing"]}]})
    except PlannerError:
        pass
    else:
        raise AssertionError("引用不存在节点的计划应该被拒绝")


def test_extract_plan_from_tool_call():
    tool_call = type(
        "ToolCall",
        (),
        {"function": type("Function", (), {"arguments": '{"nodes": []}'})()},
    )()
    assert extract_plan_from_tool_call(tool_call) == {"nodes": []}


def test_extract_invalid_plan_json_is_rejected():
    for arguments in ("not-json", "[]"):
        tool_call = type(
            "ToolCall",
            (),
            {"function": type("Function", (), {"arguments": arguments})()},
        )()
        try:
            extract_plan_from_tool_call(tool_call)
        except PlannerError:
            pass
        else:
            raise AssertionError("非法 Planner arguments 应该抛 PlannerError")


# ---------------------------------------------------------------------------
# 阶段 5-C 第二步：submit_plan 工具调用的离线测试
# 下面是一套"假 LLM 客户端"，不需要真实 API 也能测 Planner 的重试逻辑。
# ---------------------------------------------------------------------------

from app.planner import PlannerAgent, SUBMIT_PLAN_TOOL


class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, name, arguments, call_id):
        self.id = call_id
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    def __init__(self, tool_calls=None, content=None):
        self.tool_calls = tool_calls
        self.content = content


class FakeResponse:
    def __init__(self, message):
        self.choices = [type("Choice", (), {"message": message})()]


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("假 LLM 的响应已用尽：Planner 调 LLM 的次数比预期多")
        return self.responses.pop(0)


class FakeClient:
    """按顺序返回预置响应，并把每次请求参数记录在 client.completions.calls 里。"""

    def __init__(self, messages):
        self.completions = FakeCompletions([FakeResponse(m) for m in messages])
        self.chat = type("Chat", (), {"completions": self.completions})()


def plan_call(arguments, call_id="call-1"):
    """构造一条"LLM 调用了 submit_plan"的消息。"""
    return FakeMessage(tool_calls=[FakeToolCall("submit_plan", arguments, call_id)])


CYCLIC_PLAN = (
    '{"nodes": [{"id": "A", "depends_on": ["B"]},'
    ' {"id": "B", "depends_on": ["A"]}]}'
)
VALID_PLAN = '{"nodes": [{"id": "collect"}, {"id": "write", "depends_on": ["collect"]}]}'


def test_planner_builds_dag_from_submit_plan():
    """合法计划应直接转成 DAG，并且请求里带上了 submit_plan 工具。"""
    client = FakeClient([plan_call(VALID_PLAN)])
    dag = PlannerAgent(client).plan("调研并写报告")
    assert dag.ready_nodes(set()) == ["collect"]
    assert dag.ready_nodes({"collect"}) == ["write"]

    request = client.completions.calls[0]
    assert request["tools"] == [SUBMIT_PLAN_TOOL]
    # 为什么不是强制 {"type":"function","function":{"name":"submit_plan"}}？
    # 因为 DeepSeek 思考模式会返回 400 "Thinking mode does not support this tool_choice"。
    # 所以只能 "auto"，靠提示词 + 缺工具调用时的反馈兜底。
    assert request["tool_choice"] == "auto"


def test_planner_retries_with_validation_error():
    """第一次给环状计划，第二次给合法计划：应该重试成功，且错误被喂回给 LLM。"""
    client = FakeClient([plan_call(CYCLIC_PLAN, "call-1"), plan_call(VALID_PLAN, "call-2")])
    dag = PlannerAgent(client).plan("目标")
    assert dag.ready_nodes(set()) == ["collect"]
    assert len(client.completions.calls) == 2

    second_messages = client.completions.calls[1]["messages"]
    feedback = [m["content"] for m in second_messages if isinstance(m, dict) and m["role"] == "tool"]
    assert feedback and "循环" in feedback[0]


def test_planner_gives_up_after_max_attempts():
    """连续非法计划用尽次数后应抛 PlannerError，并且正好调了 max_attempts 次 LLM。"""
    client = FakeClient([plan_call(CYCLIC_PLAN, f"call-{i}") for i in range(3)])
    planner = PlannerAgent(client, max_attempts=3)
    try:
        planner.plan("目标")
    except PlannerError:
        pass
    else:
        raise AssertionError("连续非法计划应该抛 PlannerError")
    assert len(client.completions.calls) == 3
    assert planner.last_error


def test_planner_feeds_back_missing_tool_call():
    """LLM 没调工具时，应该用反馈把它拉回 submit_plan，而不是直接崩。"""
    client = FakeClient([
        FakeMessage(content="我直接用文字回答：先搜索再分析"),
        plan_call(VALID_PLAN, "call-2"),
    ])
    dag = PlannerAgent(client).plan("目标")
    assert dag.ready_nodes(set()) == ["collect"]

    second_messages = client.completions.calls[1]["messages"]
    # 坑：role="user" 的消息这次有两条（最开始的原始目标 + 追加的反馈），
    # 所以必须看最后一条。写 feedback[0] 会拿到原始目标 "目标"，断言就会假失败。
    user_messages = [m["content"] for m in second_messages if isinstance(m, dict) and m["role"] == "user"]
    assert user_messages[-1].startswith("你没有调用 submit_plan")


# TODO P6（学生任务）：自己再加 1 个测试。
# 可选方向：① 节点数超过 max_nodes 时也要走"喂回重试"；② max_attempts=1 时只调一次 LLM；
#          ③ 校验用 custom max_nodes 是否传进了 plan_to_dag。
def test_planner_retries_with_node_limit():
    """第一次让 LLM 返回 2 个节点的合法 JSON，第二次返回 1 个节点；断言：最终成功、LLM 被调用了 2 次、第二次请求里带着"节点数不能超过 1"的反馈。"""
    client = FakeClient([
        plan_call('{"nodes": [{"id": "A"}, {"id": "B"}]}', "call-1"),
        plan_call('{"nodes": [{"id": "A"}]}', "call-2"),
    ])
    planner = PlannerAgent(client, max_attempts=2, max_nodes=1)
    dag = planner.plan("目标")
    assert dag.ready_nodes(set()) == ["A"]
    assert len(client.completions.calls) == 2

    second_messages = client.completions.calls[1]["messages"]
    feedback = [m["content"] for m in second_messages if isinstance(m, dict) and m["role"] == "tool"]
    # 注意：要跟 plan_to_dag 的真实报错文案一字不差（"不能超过"），别凭记忆手打。
    assert feedback and "节点数不能超过 1" in feedback[0]

if __name__ == "__main__":
    tests = (
        test_valid_plan_becomes_dag,
        test_cycle_in_plan_is_rejected,
        test_plan_node_limit_is_enforced,
        test_unknown_dependency_is_rejected,
        test_extract_plan_from_tool_call,
        test_extract_invalid_plan_json_is_rejected,
        test_planner_builds_dag_from_submit_plan,
        test_planner_retries_with_validation_error,
        test_planner_gives_up_after_max_attempts,
        test_planner_feeds_back_missing_tool_call,
        test_planner_retries_with_node_limit,
    )
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
