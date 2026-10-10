"""agent-platform MCP server 测试：协议层（离线）+ 工具层（httpx.MockTransport 假 API）。

不需要真起 API、不需要数据库、不需要 LLM：工具层只验证"请求打到哪个端点、怎么格式化、错误怎么映射"。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app.mcp import platform_tools
from app.mcp import protocol as protocol_module
from app.mcp.platform_tools import (
    ToolError,
    build_tools,
    get_cost,
    get_trace,
    get_workflow,
    submit_workflow,
)
from app.mcp.protocol import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    ProtocolError,
)
from app.mcp.server import handle_message, tools_payload

REPO_ROOT = Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.run(coro)


def fake_tools():
    async def echo(args):
        return f"echo:{args.get('text')}"

    async def boom(_args):
        raise ToolError("业务失败：workflow 不存在")

    return [
        platform_tools.Tool(
            name="echo",
            description="回显",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            handler=echo,
        ),
        platform_tools.Tool(name="boom", description="业务失败", input_schema={}, handler=boom),
    ]


# ---------- 协议层 ----------


def test_initialize_handshake():
    response = run(
        handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, fake_tools())
    )
    assert response["result"]["protocolVersion"]
    assert response["result"]["serverInfo"]["name"] == "agent-platform"
    assert "tools" in response["result"]["capabilities"]


def test_notification_has_no_response():
    assert run(handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, fake_tools())) is None


def test_tools_list_schema_shape():
    response = run(handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, fake_tools()))
    assert [tool["name"] for tool in response["result"]["tools"]] == ["echo", "boom"]
    assert all("inputSchema" in tool for tool in response["result"]["tools"])


def test_tool_call_success_and_is_error():
    ok = run(
        handle_message(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "hi"}}},
            fake_tools(),
        )
    )
    assert ok["result"]["content"][0]["text"] == "echo:hi"
    assert ok["result"]["isError"] is False

    failed = run(
        handle_message(
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "boom"}},
            fake_tools(),
        )
    )
    assert failed["result"]["isError"] is True
    assert "workflow 不存在" in failed["result"]["content"][0]["text"]


def test_unknown_method_and_invalid_params():
    unknown = run(handle_message({"jsonrpc": "2.0", "id": 5, "method": "resources/list"}, fake_tools()))
    assert unknown["error"]["code"] == METHOD_NOT_FOUND

    invalid = run(
        handle_message(
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": 42}}, fake_tools()
        )
    )
    assert invalid["error"]["code"] == INVALID_PARAMS


def test_parse_error_code():
    with pytest.raises(ProtocolError) as excinfo:
        protocol_module.parse_message("{oops")
    assert excinfo.value.code == PARSE_ERROR


def test_tools_payload_shape():
    assert set(tools_payload(fake_tools())[0]) == {"name", "description", "inputSchema"}


def test_stdio_subprocess_stdout_is_pure_json():
    process = subprocess.Popen(
        [sys.executable, "-m", "app.mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ},
    )
    try:
        assert process.stdin and process.stdout
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n")
        process.stdin.flush()
        payload = json.loads(process.stdout.readline())
        names = [tool["name"] for tool in payload["result"]["tools"]]
        assert names == ["submit_workflow", "get_workflow", "get_cost", "get_trace"]
        process.stdin.close()
        assert process.wait(timeout=20) == 0
    finally:
        if process.poll() is None:
            process.kill()


# ---------- 工具层（假 API） ----------


@pytest.fixture
def mock_api(monkeypatch):
    """把 httpx 的请求交给假实现：记录 (method, path) 并按需返回。"""
    calls: list[tuple[str, str]] = []

    def install(handler):
        def wrapper(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            return handler(request)

        monkeypatch.setattr(platform_tools, "_transport", httpx.MockTransport(wrapper))
        return calls

    return install


def test_submit_workflow_returns_id(mock_api):
    calls = mock_api(lambda request: httpx.Response(202, json={"workflow_id": "wf-1", "status": "PLANNING"}))

    text = run(submit_workflow({"goal": "查人口并总结", "budget_usd": 0.05}))

    assert "wf-1" in text
    assert calls == [("POST", "/workflows")]


def test_submit_workflow_validates_budget(mock_api):
    mock_api(lambda request: httpx.Response(202, json={"workflow_id": "wf-1"}))

    with pytest.raises(ToolError):
        run(submit_workflow({"goal": "x", "budget_usd": -1}))


def test_get_workflow_formats_status_and_output(mock_api):
    mock_api(
        lambda request: httpx.Response(
            200, json={"workflow": {"id": "wf-1", "status": "COMPLETED", "goal": "查人口", "final_output": "北京 2100 万"}}
        )
    )

    text = run(get_workflow({"workflow_id": "wf-1"}))

    assert "COMPLETED" in text
    assert "北京 2100 万" in text


def test_get_cost_formats_numbers(mock_api):
    mock_api(
        lambda request: httpx.Response(
            200, json={"input_tokens": 6382, "output_tokens": 3478, "cost_usd": 0.005393, "budget_usd": 0.05}
        )
    )

    text = run(get_cost({"workflow_id": "wf-1"}))

    assert "6382" in text and "0.005393" in text and "0.05" in text


def test_get_trace_formats_critical_path(mock_api):
    mock_api(
        lambda request: httpx.Response(
            200,
            json={
                "duration_ms": 21559,
                "critical_path": ["a", "b"],
                "critical_path_ms": 12856,
                "slowest_node": {"node_id": "b", "duration_ms": 7493, "llm_ms": 7400},
                "nodes": [{"node_id": "a", "status": "COMPLETED", "attempts": 1}],
            },
        )
    )

    text = run(get_trace({"workflow_id": "wf-1"}))

    assert "a → b" in text
    assert "7493" in text and "7400" in text


def test_api_error_status_becomes_tool_error(mock_api):
    mock_api(lambda request: httpx.Response(404, text="workflow not found"))

    with pytest.raises(ToolError) as excinfo:
        run(get_workflow({"workflow_id": "missing"}))

    assert "404" in str(excinfo.value)


def test_unreachable_api_becomes_tool_error(monkeypatch):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(platform_tools, "_transport", httpx.MockTransport(boom))

    with pytest.raises(ToolError) as excinfo:
        run(get_cost({"workflow_id": "wf-1"}))

    assert "不可达" in str(excinfo.value)


def test_control_tools_hidden_by_default(monkeypatch):
    monkeypatch.delenv("MCP_ALLOW_CONTROL", raising=False)
    assert "decide_approval" not in [tool.name for tool in build_tools()]

    monkeypatch.setenv("MCP_ALLOW_CONTROL", "1")
    names = [tool.name for tool in build_tools()]
    assert "decide_approval" in names and "list_pending_approvals" in names


def test_decide_approval_requires_flag(mock_api):
    mock_api(lambda request: httpx.Response(200, json={"task_status": "PENDING"}))

    with pytest.raises(ToolError):
        run(platform_tools.decide_approval({"approval_id": "a-1", "approved": True}))



def test_stdout_is_taken_over_for_protocol():
    """fd 级接管：print 不再进协议通道，协议流仍然干净。"""
    import os
    import sys as _sys

    from app.mcp.server import take_over_stdout

    saved_fd = os.dup(1)
    saved_stdout, saved_stderr = _sys.stdout, _sys.stderr
    read_fd, write_fd = os.pipe()
    try:
        os.dup2(write_fd, 1)
        os.close(write_fd)
        _sys.stdout = os.fdopen(os.dup(1), "w", buffering=1)

        protocol_out = take_over_stdout()
        print("这行是普通输出，应该走 stderr")  # noqa: T201
        protocol_out.write('{"jsonrpc":"2.0","id":1}\n')
        protocol_out.flush()

        os.set_blocking(read_fd, False)
        captured = os.read(read_fd, 4096).decode()
        assert captured.strip() == '{"jsonrpc":"2.0","id":1}'
    finally:
        os.dup2(saved_fd, 1)
        os.close(saved_fd)
        os.close(read_fd)
        _sys.stdout, _sys.stderr = saved_stdout, saved_stderr
