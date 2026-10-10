"""把外部 MCP server 的工具注册进本平台。

注册规则：
- 工具名统一加前缀 `mcp__<server>__<tool>`，避免和内置工具（calculator / search）撞名，
  也让 `/trace` 里一眼看出"这一步是外部工具"；
- **启动失败不拦平台**（fail-open）：某个 MCP server 起不来只在日志里留痕，平台照常启动；
- **调用失败返回错误文本**（fail-soft）：`TOOLS` 里的工具约定返回字符串，
  错误文本会作为 observation 回到 Agent 循环，无进展检测与预算记账继续正常工作。

配置来自 `MCP_SERVERS`（JSON 数组）：

```json
[{"name": "rag", "command": ["python", "-m", "app.mcp.server"],
  "env": {"MCP_USER_EMAIL": "me@example.com"}, "timeout_seconds": 20,
  "allowed_tools": ["search_knowledge", "ask_knowledge"]}]
```
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from app.mcp.client import MCPClient, MCPClientError, MCPServerConfig, extract_text
from app.tools import TOOLS
from app.tools import tools as TOOL_SCHEMAS

logger = logging.getLogger(__name__)

_CLIENTS: dict[str, MCPClient] = {}


def parse_config(raw: str | None) -> list[MCPServerConfig]:
    """解析 MCP_SERVERS；非法条目跳过并记日志（配置错不该让进程起不来）。"""
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("mcp_config_not_json", extra={"error": str(exc)})
        return []
    if not isinstance(data, list):
        logger.error("mcp_config_not_list")
        return []

    configs: list[MCPServerConfig] = []
    for item in data:
        if not isinstance(item, dict):
            logger.error("mcp_config_entry_not_object")
            continue
        try:
            configs.append(MCPServerConfig.from_dict(item))
        except MCPClientError as exc:
            logger.error("mcp_config_entry_invalid", extra={"error": str(exc)})
    return configs


def _make_handler(client: MCPClient, tool_name: str) -> Callable[..., str]:
    """生成注册进 TOOLS 的同步 handler：平台是同步调用工具，MCP 调用也在这里同步等结果。"""

    def handler(**kwargs: Any) -> str:
        try:
            result = client.call_tool(tool_name, kwargs)
        except MCPClientError as exc:
            logger.warning(
                "mcp_tool_call_failed",
                extra={"server": client.config.name, "tool": tool_name, "error": str(exc)},
            )
            return f"错误：MCP 工具 {tool_name}（{client.config.name}）调用失败：{exc}"

        text = extract_text(result)
        if result.get("isError"):
            return f"错误：{text}"
        return text or f"（MCP 工具 {tool_name} 返回了空内容）"

    # get_tool_descriptions() 用 __doc__ 生成提示词文本，这里必须补上。
    handler.__doc__ = f"MCP 工具（{client.config.name}）：{tool_name}"
    handler.__name__ = f"mcp_{client.config.name}_{tool_name}".replace("-", "_")
    return handler


def register_mcp_tools(raw: str | None = None, configs: list[MCPServerConfig] | None = None) -> list[str]:
    """连接并注册 MCP 工具，返回注册成功的完整工具名列表。"""
    if configs is None:
        configs = parse_config(raw)

    registered: list[str] = []
    for config in configs:
        client = MCPClient(config)
        try:
            available = client.list_tools()
        except MCPClientError as exc:
            # fail-open：外部 server 挂了不该拦住平台启动；调用时再按工具粒度失败。
            logger.warning(
                "mcp_server_unavailable_at_startup",
                extra={"server": config.name, "error": str(exc)},
            )
            client.close()
            continue

        count = 0
        for tool in available:
            tool_name = tool["name"]
            if config.allowed_tools and tool_name not in config.allowed_tools:
                continue
            full_name = f"mcp__{config.name}__{tool_name}"
            TOOLS[full_name] = _make_handler(client, tool_name)
            TOOL_SCHEMAS.append(
                {
                    "type": "function",
                    "function": {
                        "name": full_name,
                        "description": f"[MCP:{config.name}] {tool.get('description', '')}".strip(),
                        "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
                    },
                }
            )
            registered.append(full_name)
            count += 1

        _CLIENTS[config.name] = client
        logger.info(
            "mcp_server_registered",
            extra={"server": config.name, "tools": count, "allowed_tools": config.allowed_tools},
        )

    return registered


def registered_servers() -> dict[str, MCPClient]:
    return dict(_CLIENTS)


def shutdown_mcp_clients() -> None:
    for client in _CLIENTS.values():
        client.close()
    _CLIENTS.clear()
