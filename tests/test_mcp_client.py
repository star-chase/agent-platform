"""MCP 客户端与桥接层测试。

全部用假 server 子进程（`tests/fake_mcp_server.py`），离线可跑：不需要 LLM、不需要数据库、不需要网络。
覆盖的是"出事的时候会怎样"：超时、子进程崩溃、脏 stdout、server 起不来。
"""

from __future__ import annotations

import sys

import pytest

from app.mcp.bridge import (
    parse_config,
    register_mcp_tools,
    registered_servers,
    shutdown_mcp_clients,
)
from app.mcp.client import MCPClient, MCPClientError, MCPServerConfig, extract_text
from app.tools import TOOLS
from app.tools import tools as TOOL_SCHEMAS


def make_config(**overrides) -> MCPServerConfig:
    raw = {
        "name": "fake",
        "command": [sys.executable, "-m", "tests.fake_mcp_server"],
        "timeout_seconds": 10,
    }
    raw.update(overrides)
    return MCPServerConfig.from_dict(raw)


@pytest.fixture(autouse=True)
def restore_registry():
    """TOOLS / tools 是模块级全局，测试必须还原，否则会污染其它用例。"""
    tools_before = dict(TOOLS)
    schemas_before = list(TOOL_SCHEMAS)
    yield
    TOOLS.clear()
    TOOLS.update(tools_before)
    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.extend(schemas_before)
    shutdown_mcp_clients()


def test_handshake_and_tools_list():
    client = MCPClient(make_config())
    try:
        names = [tool["name"] for tool in client.list_tools()]
        assert names == ["echo", "boom", "slow", "die", "noisy"]
    finally:
        client.close()


def test_call_tool_returns_text():
    client = MCPClient(make_config())
    try:
        result = client.call_tool("echo", {"text": "hi"})
        assert extract_text(result) == "echo:hi"
        assert result["isError"] is False
    finally:
        client.close()


def test_tool_error_is_returned_not_raised():
    """业务失败是 result.isError，客户端不该把它当传输错误抛出去。"""
    client = MCPClient(make_config())
    try:
        result = client.call_tool("boom", {})
        assert result["isError"] is True
        assert "知识库不存在" in extract_text(result)
    finally:
        client.close()


def test_timeout_kills_process_and_recovers():
    client = MCPClient(make_config(timeout_seconds=1))
    try:
        with pytest.raises(MCPClientError, match="超时"):
            client.call_tool("slow", {"seconds": 5})
        # 下一次调用自动重启，平台不会因为一次超时就永久失去这个工具
        assert extract_text(client.call_tool("echo", {"text": "after-timeout"})) == "echo:after-timeout"
    finally:
        client.close()


def test_crashed_server_is_reported_and_restartable():
    client = MCPClient(make_config())
    try:
        with pytest.raises(MCPClientError):
            client.call_tool("die", {})
        assert extract_text(client.call_tool("echo", {"text": "again"})) == "echo:again"
    finally:
        client.close()


def test_noisy_stdout_is_tolerated():
    """有的 server 会把日志写进 stdout：客户端跳过脏行但要继续等真正的响应。"""
    client = MCPClient(make_config())
    try:
        assert extract_text(client.call_tool("noisy", {})) == "noisy ok"
    finally:
        client.close()


def test_bridge_registers_prefixed_tools():
    registered = register_mcp_tools(configs=[make_config()])

    assert "mcp__fake__echo" in registered
    assert "mcp__fake__echo" in TOOLS
    schema = next(item for item in TOOL_SCHEMAS if item["function"]["name"] == "mcp__fake__echo")
    assert schema["function"]["parameters"]["type"] == "object"
    assert schema["function"]["description"].startswith("[MCP:fake]")
    assert registered_servers().keys() == {"fake"}

    # 平台是同步调用工具：TOOLS[name](**args) -> 字符串
    assert TOOLS["mcp__fake__echo"](text="hi") == "echo:hi"


def test_bridge_maps_is_error_to_error_text():
    register_mcp_tools(configs=[make_config()])

    assert TOOLS["mcp__fake__boom"]().startswith("错误：业务失败")


def test_bridge_honours_allowed_tools():
    registered = register_mcp_tools(configs=[make_config(allowed_tools=["echo"])])

    assert registered == ["mcp__fake__echo"]
    assert not any(item["function"]["name"] == "mcp__fake__boom" for item in TOOL_SCHEMAS)


def test_dead_server_at_startup_does_not_block():
    """fail-open：外部 server 起不来，平台照常启动，只是少一个工具。"""
    registered = register_mcp_tools(
        configs=[make_config(name="ghost", command=["definitely-not-a-real-binary-xyz"])]
    )

    assert registered == []
    assert "ghost" not in registered_servers()


def test_parse_config_is_defensive():
    assert parse_config(None) == []
    assert parse_config("   ") == []
    assert parse_config("{not json") == []
    assert parse_config('{"name": "not-a-list"}') == []
    assert parse_config('[{"name": "ok", "command": ["echo"]}, {"command": []}]')[0].name == "ok"


def test_extract_text_ignores_non_text_parts():
    result = {"content": [{"type": "text", "text": "a"}, {"type": "image", "data": "x"}, {"type": "text", "text": "b"}]}
    assert extract_text(result) == "a\nb"
