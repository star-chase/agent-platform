"""MCP 协议的 JSON-RPC 2.0 编解码层（stdio 传输）。

只实现 tools 能力需要的子集：initialize / notifications/initialized / ping / tools/list / tools/call。

两条容易踩错、这里刻意写死的规则：
1. **stdio 是换行分隔的 JSON-RPC**（不是 LSP 的 `Content-Length` 分帧）——一行一个完整消息；
2. **stdout 只允许出现协议消息**，日志一律走 stderr（loguru 默认就是 stderr）。

设计取舍：不引入官方 `mcp` SDK。协议层只有两百行，手写换来的是"零新依赖 + 能离线测协议本身"，
和本仓库其它模块（分块器、混合检索、RRF）的手写风格一致。
"""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = "2025-06-18"

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ProtocolError(Exception):
    """JSON-RPC 层错误：编解码失败、方法不存在、参数不合法。

    注意与 `tools.ToolError` 的区别：**工具执行失败不是协议错误** ——
    按 MCP 约定返回正常 result + `isError: true`，让模型能看到原因并自己决定下一步。
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def dumps(message: dict[str, Any]) -> str:
    """序列化成一行（ensure_ascii=False 让中文可读，便于人工排查）。"""
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def parse_message(line: str) -> dict[str, Any]:
    """把一行文本解析成 JSON-RPC 消息；失败抛 ProtocolError(-32700 / -32600)。"""
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(PARSE_ERROR, "JSON 解析失败", {"detail": str(exc)}) from exc
    if not isinstance(message, dict):
        raise ProtocolError(INVALID_REQUEST, "消息必须是 JSON 对象")
    if message.get("jsonrpc") != "2.0":
        raise ProtocolError(INVALID_REQUEST, "jsonrpc 字段必须是 \"2.0\"")
    if not isinstance(message.get("method"), str):
        raise ProtocolError(INVALID_REQUEST, "缺少 method 字段")
    params = message.get("params")
    if params is not None and not isinstance(params, dict):
        raise ProtocolError(INVALID_PARAMS, "params 必须是对象")
    return message


def is_notification(message: dict[str, Any]) -> bool:
    """没有 id 的是通知：按协议**不能回响应**。"""
    return "id" not in message


def success(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def failure(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def tool_result(request_id: Any, text: str, is_error: bool = False) -> dict[str, Any]:
    """tools/call 的返回：content 数组 + isError。

    工具失败也走这里（`is_error=True`），不走 JSON-RPC error —— 模型看到的是内容，
    这样才能"失败也可解释"，而不是整个协议调用崩掉。
    """
    return success(
        request_id,
        {"content": [{"type": "text", "text": text}], "isError": is_error},
    )
