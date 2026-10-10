"""测试用假 MCP server：覆盖正常调用、业务失败、超时、崩溃、脏 stdout 五种情况。

跑法：`python -m tests.fake_mcp_server`，通过 stdio 讲 JSON-RPC。
"""

from __future__ import annotations

import json
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "回显文本",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {"name": "boom", "description": "总是业务失败", "inputSchema": {"type": "object", "properties": {}}},
    {
        "name": "slow",
        "description": "睡一会儿再回答",
        "inputSchema": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
            "required": ["seconds"],
        },
    },
    {"name": "die", "description": "直接退出进程", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "noisy", "description": "先写一行脏 stdout 再回答", "inputSchema": {"type": "object", "properties": {}}},
]


def _write(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _text(rid, text: str, is_error: bool = False) -> None:
    _write({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}], "isError": is_error}})


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        rid = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}

        if method == "initialize":
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-mcp", "version": "0.1.0"},
                    },
                }
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _write({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "echo":
                _text(rid, f"echo:{arguments.get('text')}")
            elif name == "boom":
                _text(rid, "业务失败：知识库不存在", is_error=True)
            elif name == "slow":
                time.sleep(float(arguments.get("seconds", 5)))
                _text(rid, "slow done")
            elif name == "noisy":
                sys.stdout.write("这是一行混进 stdout 的日志\n")
                sys.stdout.flush()
                _text(rid, "noisy ok")
            elif name == "die":
                sys.exit(3)
            else:
                _text(rid, f"未知工具：{name}", is_error=True)
        else:
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
