"""阶段 5-E：Agent 角色档案测试。

分两层：
1. 纯结构测试——三个角色是否齐全、工具名是否真实存在、提示词是否非空；
2. 执行器注入测试——system_prompt / tool_names 有没有真的作用到 LLM 调用上。
"""

from app.agent_fc_persistent import AgentFCPersistentExecutor
from app.agent_profiles import AGENT_PROFILES, DEFAULT_AGENT_TYPE, get_profile
from app.tools import TOOLS
from tests.test_parallel_tool_calls import FINAL_MESSAGE, FakeClient, make_db


# ---------------------------------------------------------------------------
# 第 1 层：角色档案本身
# ---------------------------------------------------------------------------

def test_all_expected_profiles_exist():
    assert {"researcher", "analyst", "writer"} <= set(AGENT_PROFILES)


def test_profile_tools_reference_real_tools():
    """档案里写的工具名必须真的存在，否则运行时 _call_tool 只会返回错误字符串。"""
    for name, profile in AGENT_PROFILES.items():
        unknown = set(profile.tools) - set(TOOLS)
        assert not unknown, f"{name} 引用了不存在的工具：{sorted(unknown)}"


def test_every_profile_has_a_system_prompt():
    for name, profile in AGENT_PROFILES.items():
        assert profile.system_prompt.strip(), f"{name} 缺少系统提示词"


def test_writer_has_no_tools_others_have_some():
    """writer 只负责组织上游结论，不该再自己去找资料。"""
    assert AGENT_PROFILES["writer"].tools == ()
    assert AGENT_PROFILES["researcher"].tools
    assert AGENT_PROFILES["analyst"].tools


def test_get_profile_falls_back_to_default_for_unknown_type():
    assert get_profile("writer").name == "writer"
    assert get_profile("不存在的角色").name == DEFAULT_AGENT_TYPE
    assert get_profile(None).name == DEFAULT_AGENT_TYPE


# ---------------------------------------------------------------------------
# 第 2 层：执行器真的用上了角色档案
# ---------------------------------------------------------------------------

def make_executor(tmp_path, **kwargs):
    db_path = make_db(tmp_path)
    client = FakeClient([FINAL_MESSAGE])
    executor = AgentFCPersistentExecutor(
        llm_client=client, model="fake-model", db_path=db_path, **kwargs
    )
    return executor, client


def test_executor_uses_injected_system_prompt(tmp_path):
    profile = AGENT_PROFILES["writer"]
    executor, client = make_executor(
        tmp_path, system_prompt=profile.system_prompt, tool_names=profile.tools
    )
    executor.run("把上游结论写成报告")

    sent_system = client.completions.calls[0]["messages"][0]
    assert sent_system["role"] == "system"
    # 角色提示词在最前面；后面**一定**追加控制面规则（阶段 6-D 的审批规则对所有角色
    # 生效，所以不能再拿等号断言整个字符串）
    assert sent_system["content"].startswith(profile.system_prompt)
    assert "request_human_approval" in sent_system["content"]


# 阶段 6-D 的控制面工具：请求人工审批。它**不参与角色裁剪**——
# 恰恰是 writer 这种"要对外发布"的角色最需要它。
CONTROL_TOOL = "request_human_approval"


def test_executor_filters_tools_by_profile(tmp_path):
    executor, client = make_executor(tmp_path, tool_names=("calculator",))
    executor.run("算一下")

    names = [tool["function"]["name"] for tool in client.completions.calls[0]["tools"]]
    assert names == ["calculator", CONTROL_TOOL]


def test_tool_less_profile_still_gets_the_control_tool(tmp_path):
    """角色可以没有业务工具，但一定拿得到控制面工具（否则它没法请人审批）。

    契约变化（阶段 6-D）：以前"无工具角色"连 tools 参数都不传；
    现在传且只传控制面工具 —— 它不违反"传 tools=[] 会被 400"那条坑（数组非空）。
    """
    executor, client = make_executor(tmp_path, tool_names=())
    executor.run("写报告")

    names = [tool["function"]["name"] for tool in client.completions.calls[0]["tools"]]
    assert names == [CONTROL_TOOL]


def test_executor_without_profile_keeps_all_tools(tmp_path):
    """不传角色档案时保持旧行为：全部业务工具可用（兼容阶段 1-4 的老调用）。"""
    executor, client = make_executor(tmp_path)
    executor.run("随便问点什么")

    names = {tool["function"]["name"] for tool in client.completions.calls[0]["tools"]}
    assert names == set(TOOLS) | {CONTROL_TOOL}
