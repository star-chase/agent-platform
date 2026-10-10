"""本平台作为 MCP server 时暴露的工具。

薄适配层：所有工具都调本机 HTTP API（`AGENT_PLATFORM_API`），**不复制业务逻辑** ——
否则端点改了、MCP 工具没跟着改，就会出现"两条口径"的 bug。

写操作（审批）默认不挂载，需要 `MCP_ALLOW_CONTROL=1`：能替人批准动作的工具，
不该因为"配置里有这个 server"就自动生效。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_API_BASE = "http://localhost:8000"
DEFAULT_TIMEOUT = 30.0
MAX_TEXT_CHARS = 4000

# 测试注入点：httpx.MockTransport 会把所有请求交给假实现，不需要真起 API。
_transport: httpx.BaseTransport | None = None


class ToolError(Exception):
    """业务失败 → `isError: true`（与 rag-agent 侧同一套约定）。"""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[str]]

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


def _api_base() -> str:
    return os.environ.get("AGENT_PLATFORM_API", DEFAULT_API_BASE).rstrip("/")


def _truncate(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n…（已截断，原始 {len(text)} 字符）"


async def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    base = _api_base()
    timeout = float(os.environ.get("MCP_API_TIMEOUT", DEFAULT_TIMEOUT))
    try:
        with httpx.Client(base_url=base, timeout=timeout, transport=_transport) as client:
            response = client.request(method, path, json=payload)
    except httpx.HTTPError as exc:
        raise ToolError(f"平台 API 不可达（{base}）：{exc}") from exc

    if response.status_code >= 400:
        detail = response.text[:200].replace("\n", " ")
        raise ToolError(f"平台 API 返回 {response.status_code}：{detail}")
    try:
        return response.json()
    except ValueError as exc:
        raise ToolError(f"平台 API 返回的不是 JSON：{response.text[:120]}") from exc


def _require_str(args: dict[str, Any], key: str, max_len: int = 4000) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{key} 不能为空")
    if len(value) > max_len:
        raise ToolError(f"{key} 太长（上限 {max_len} 字符）")
    return value.strip()


async def submit_workflow(args: dict[str, Any]) -> str:
    goal = _require_str(args, "goal")
    payload: dict[str, Any] = {"goal": goal}
    budget = args.get("budget_usd")
    if budget is not None:
        if not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget <= 0:
            raise ToolError("budget_usd 必须是正数")
        payload["budget_usd"] = float(budget)

    data = await _request("POST", "/workflows", payload)
    workflow_id = data.get("workflow_id") or (data.get("workflow") or {}).get("id")
    if not workflow_id:
        raise ToolError(f"提交成功但响应里没有 workflow_id：{str(data)[:200]}")
    return f"已提交 workflow {workflow_id}，用 get_workflow / get_cost / get_trace 查状态。"


async def get_workflow(args: dict[str, Any]) -> str:
    workflow_id = _require_str(args, "workflow_id", max_len=64)
    data = await _request("GET", f"/workflows/{workflow_id}")
    workflow = data.get("workflow") or data
    lines = [f"workflow {workflow_id} 状态：{workflow.get('status')}"]
    if workflow.get("goal"):
        lines.append(f"目标：{workflow['goal']}")
    if workflow.get("final_output"):
        lines.append(f"最终产出：\n{workflow['final_output']}")
    return _truncate("\n".join(lines))


async def get_cost(args: dict[str, Any]) -> str:
    workflow_id = _require_str(args, "workflow_id", max_len=64)
    data = await _request("GET", f"/workflows/{workflow_id}/cost")
    return (
        f"workflow {workflow_id}：输入 {data.get('input_tokens')} token / "
        f"输出 {data.get('output_tokens')} token，成本 ${data.get('cost_usd')}，"
        f"预算 ${data.get('budget_usd')}。"
    )


async def get_trace(args: dict[str, Any]) -> str:
    workflow_id = _require_str(args, "workflow_id", max_len=64)
    data = await _request("GET", f"/workflows/{workflow_id}/trace")
    slowest = data.get("slowest_node") or {}
    lines = [
        f"workflow {workflow_id} 总耗时 {data.get('duration_ms')} ms",
        (
            f"关键路径：{' → '.join(data.get('critical_path') or [])}"
            f"（{data.get('critical_path_ms')} ms）"
        ),
        (
            f"最慢节点：{slowest.get('node_id')} duration={slowest.get('duration_ms')} ms，"
            f"其中模型延迟 {slowest.get('llm_ms')} ms"
        ),
        "节点："
        + "、".join(
            f"{node['node_id']}({node['status']},{node['attempts']} 次)"
            for node in data.get("nodes", [])
        ),
    ]
    return _truncate("\n".join(lines))


async def list_pending_approvals(args: dict[str, Any]) -> str:
    _require_control()
    data = await _request("GET", "/approvals/pending")
    items = data.get("approvals") or data.get("items") or []
    if not items:
        return "当前没有待审批项。"
    return "\n".join(
        f"- approval_id={item.get('id')} task={item.get('task_id')} 原因={item.get('reason')}"
        for item in items
    )


async def decide_approval(args: dict[str, Any]) -> str:
    _require_control()
    approval_id = _require_str(args, "approval_id", max_len=64)
    approved = args.get("approved")
    if not isinstance(approved, bool):
        raise ToolError("approved 必须是 true / false")
    payload: dict[str, Any] = {"approved": approved}
    if args.get("comment"):
        payload["comment"] = str(args["comment"])[:500]
    data = await _request("POST", f"/approvals/{approval_id}/decide", payload)
    return f"审批 {approval_id} 已处理：{data.get('task_status') or data}"


def _require_control() -> None:
    if os.environ.get("MCP_ALLOW_CONTROL") != "1":
        raise ToolError("控制面未开启：需要设置 MCP_ALLOW_CONTROL=1 才能查看/处理审批")


def build_tools(include_control: bool | None = None) -> list[Tool]:
    tools = [
        Tool(
            name="submit_workflow",
            description="提交一条新的 workflow（自然语言目标），返回 workflow_id。",
            input_schema={
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "要达成的目标"},
                    "budget_usd": {"type": "number", "description": "可选：这条流水线的成本上限"},
                },
                "required": ["goal"],
            },
            handler=submit_workflow,
        ),
        Tool(
            name="get_workflow",
            description="查询 workflow 的状态与最终产出。",
            input_schema={
                "type": "object",
                "properties": {"workflow_id": {"type": "string"}},
                "required": ["workflow_id"],
            },
            handler=get_workflow,
        ),
        Tool(
            name="get_cost",
            description="查询 workflow 的 token 与成本（含预算）。",
            input_schema={
                "type": "object",
                "properties": {"workflow_id": {"type": "string"}},
                "required": ["workflow_id"],
            },
            handler=get_cost,
        ),
        Tool(
            name="get_trace",
            description="查询 workflow 的时间线：关键路径、最慢节点、每个节点的耗时与尝试次数。",
            input_schema={
                "type": "object",
                "properties": {"workflow_id": {"type": "string"}},
                "required": ["workflow_id"],
            },
            handler=get_trace,
        ),
    ]

    if include_control is None:
        include_control = os.environ.get("MCP_ALLOW_CONTROL") == "1"
    if include_control:
        tools += [
            Tool(
                name="list_pending_approvals",
                description="列出等待人工审批的动作（控制面，需要显式开启）。",
                input_schema={"type": "object", "properties": {}},
                handler=list_pending_approvals,
            ),
            Tool(
                name="decide_approval",
                description="批准或拒绝一个待审批动作（控制面，需要显式开启）。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "approval_id": {"type": "string"},
                        "approved": {"type": "boolean"},
                        "comment": {"type": "string"},
                    },
                    "required": ["approval_id", "approved"],
                },
                handler=decide_approval,
            ),
        ]
    return tools
