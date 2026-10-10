"""MCP server（stdio）：把本平台的能力暴露成标准工具。

    python -m app.mcp.server                # 跑 stdio 服务（给别的 Agent / MCP 客户端连）
    python -m app.mcp.server --list-tools   # 打印工具 schema（自检 / CI 用）

与 rag-agent 侧的 `app/mcp/server.py` 是同一套协议实现（刻意复制，不跨仓库依赖），
区别只在工具表与 serverInfo。工具通过 HTTP API 调本机平台（`AGENT_PLATFORM_API`）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
from typing import Any

from app.mcp import protocol
from app.mcp.platform_tools import Tool, ToolError, build_tools
from app.mcp.protocol import ProtocolError

SERVER_NAME = "agent-platform"
SERVER_VERSION = "0.1.0"
INSTRUCTIONS = (
    "agent-platform 编排能力：submit_workflow（提交目标）、get_workflow / get_cost / get_trace"
    "（看状态、成本、时间线）；控制面工具（审批）需要显式开启。"
)


def tools_payload(tools: list[Tool]) -> list[dict[str, Any]]:
    return [tool.to_wire() for tool in tools]


def take_over_stdout() -> Any:
    """把 fd 1 让给协议：协议写 dup 出来的原始 fd，其它标准输出全部改道 stderr。

    为什么必须做到 fd 级（实测踩过两次）：
    1. `print()` / loguru / uvicorn 之类在 Python 层写 `sys.stdout`，换 `sys.stdout` 能拦住；
    2. 但 SQLAlchemy 的 `echo=True` 在 **engine 创建时**（import 期）就抓住了原始 stdout 对象，
       之后它一直往那条老通道写 —— 只换 `sys.stdout` 拦不住，协议里就混进了 SQL 日志。
    所以这里 dup 出协议专用流，再用 `os.dup2` 把 fd 1（真正的标准输出）指向 stderr。
    """
    protocol_out = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1, encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr  # 让 print / 新建的 StreamHandler 也走 stderr
    return protocol_out


async def handle_message(message: dict[str, Any], tools: list[Tool]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message["method"]

    if protocol.is_notification(message):
        return None

    if method == "initialize":
        params = message.get("params") or {}
        return protocol.success(
            request_id,
            {
                "protocolVersion": protocol.PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS
                + (
                    f"（客户端协议版本 {params.get('protocolVersion')}，"
                    f"本服务按 {protocol.PROTOCOL_VERSION} 应答）"
                    if params.get("protocolVersion")
                    else ""
                ),
            },
        )

    if method == "ping":
        return protocol.success(request_id, {})

    if method == "tools/list":
        return protocol.success(request_id, {"tools": tools_payload(tools)})

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return protocol.failure(
                request_id, protocol.INVALID_PARAMS, "params 需要 {name: string, arguments: object}"
            )
        tool = next((item for item in tools if item.name == name), None)
        if tool is None:
            available = "、".join(item.name for item in tools)
            return protocol.tool_result(
                request_id, f"未知工具：{name}。可用工具：{available}", is_error=True
            )
        try:
            text = await tool.handler(arguments)
        except ToolError as exc:
            return protocol.tool_result(request_id, str(exc), is_error=True)
        except Exception as exc:  # noqa: BLE001 —— 兜底：任何异常都不能把 server 打挂
            return protocol.tool_result(
                request_id, f"工具执行失败：{type(exc).__name__}: {exc}", is_error=True
            )
        return protocol.tool_result(request_id, text)

    return protocol.failure(request_id, protocol.METHOD_NOT_FOUND, f"不支持的方法：{method}")


async def serve(tools: list[Tool]) -> int:
    protocol_out = take_over_stdout()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def reader() -> None:
        for line in sys.stdin:
            loop.call_soon_threadsafe(queue.put_nowait, line)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=reader, name="mcp-stdin", daemon=True).start()

    while True:
        line = await queue.get()
        if line is None:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            message = protocol.parse_message(line)
            response = await handle_message(message, tools)
        except ProtocolError as exc:
            response = protocol.failure(None, exc.code, exc.message, exc.data)
        except Exception as exc:  # noqa: BLE001
            response = protocol.failure(
                None, protocol.INTERNAL_ERROR, f"服务内部错误：{type(exc).__name__}: {exc}"
            )
        if response is not None:
            protocol_out.write(protocol.dumps(response) + "\n")
            protocol_out.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="agent-platform MCP server（stdio）")
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="打印工具 schema（JSON）后退出：自检用，不进入协议会话",
    )
    args = parser.parse_args(argv)

    tools = build_tools()
    if args.list_tools:
        print(protocol.dumps({"tools": tools_payload(tools)}))
        return 0

    return asyncio.run(serve(tools))


if __name__ == "__main__":
    raise SystemExit(main())
