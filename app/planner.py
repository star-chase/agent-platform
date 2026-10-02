import json
from typing import Any

from app.dag import DAG, DAGError


class PlannerError(ValueError):
    """Planner 输出无法转换为合法 DAG。"""


def plan_to_dag(plan: dict[str, Any], max_nodes: int = 10) -> DAG:
    """把 Planner 返回的节点 JSON 转成并校验 DAG。"""
    if not isinstance(plan, dict) or not isinstance(plan.get("nodes"), list):
        raise PlannerError("plan必须是字典,且plan['nodes']必须是列表")
    if len(plan["nodes"]) > max_nodes:
        raise PlannerError(f"节点数不能超过 {max_nodes}")
    dag = DAG()
    try:
        node_ids = set()
        for node in plan["nodes"]:
            if not isinstance(node, dict) or not isinstance(node.get("id"), str):
                raise PlannerError("每个节点必须有字符串 id")
            if node["id"] in node_ids:
                raise PlannerError(f"节点 id {node['id']} 重复")
            node_ids.add(node["id"])
            dag.add_node(node["id"], node)
        for node in plan["nodes"]:
            depends_on = node.get("depends_on", [])
            if not isinstance(depends_on, list) or not all(isinstance(item, str) for item in depends_on):
                raise PlannerError(f"节点 {node['id']} 的 depends_on 必须是字符串列表")
            for dependency in depends_on:
                dag.add_edge(dependency, node["id"])
        dag.validate()
    except DAGError as error:
        raise PlannerError(str(error)) from error
    return dag


def extract_plan_from_tool_call(tool_call: Any) -> dict[str, Any]:
    """从 submit_plan 工具调用中提取计划 JSON。"""
    try:
        plan = json.loads(tool_call.function.arguments)
    except (TypeError, json.JSONDecodeError) as error:
        raise PlannerError("Planner 返回的 arguments 不是合法 JSON") from error
    if not isinstance(plan, dict):
        raise PlannerError("Planner 计划必须是 JSON 对象")
    return plan


def dag_to_plan(dag: DAG) -> dict[str, Any]:
    """把 DAG 对象还原成计划 JSON，用于落库。

    节点 payload 本来就是计划里的那个 dict，这里只把 depends_on 换成
    DAG 里记录的依赖集合（排序后），保证落库内容和图结构一致。
    """
    return {
        "nodes": [
            {**dag.nodes[node_id], "depends_on": sorted(dag.dependencies[node_id])}
            for node_id in dag.nodes
        ]
    }


# ---------------------------------------------------------------------------
# 阶段 5-C：submit_plan 工具调用
# ---------------------------------------------------------------------------

# submit_plan 工具：用 Function Calling 强制 Planner 输出结构化 JSON。
# 这个 schema 就是"计划"的契约，LLM 会按它生成 arguments。
SUBMIT_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_plan",
        "description": "提交任务拆解计划：把用户目标拆成若干有依赖关系的子任务节点",
        "parameters": {
            "type": "object",
            "properties": {
                "nodes": {
                    "type": "array",
                    "description": "子任务节点列表，最多 10 个",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "节点唯一标识，如 collect_market"},
                            "description": {"type": "string", "description": "这个子任务要干什么，会交给另一个 Agent 执行"},
                            "agent_type": {
                                "type": "string",
                                "description": "执行者角色，只允许 'researcher' / 'analyst' / 'writer'",
                                "enum": ["researcher", "analyst", "writer"],
                            },
                            "depends_on": {
                                "type": "array",
                                "description": "前置节点的 id 列表；没有依赖就传 []",
                                "items": {"type": "string"},
                            },
                            # 阶段 6-B-2：让 Planner 自己标出"危险步骤"。
                            # 注意这不是必填项 —— 省略即视为 false（默认不需要审批）。
                            "requires_approval": {
                                "type": "boolean",
                                "description": (
                                    "这一步是否必须先由人类批准才能执行。"
                                    "只对不可逆或对外的动作设 true：对外发布/发送、发邮件或消息、花钱、"
                                    "删除数据、写入外部系统。"
                                    "查询、分析、写作这类只读或可撤销的步骤不要设（省略即可）。"
                                ),
                            },
                        },
                        "required": ["id", "description", "agent_type", "depends_on"],
                    },
                }
            },
            "required": ["nodes"],
        },
    },
}


PLANNER_SYSTEM_PROMPT = (
    "你是任务规划者。用户会给你一个复杂目标，请把它拆解成若干子任务节点，"
    "并调用 submit_plan 工具提交计划。\n"
    "规划规则：\n"
    "1. 每个节点是一个独立可执行的子任务，交给另一个 Agent 完成，所以 description 要写清楚。\n"
    "2. depends_on 只能引用本次计划里出现过的节点 id；没有先后关系的节点不要互相依赖，它们会被并行执行。\n"
    "3. 节点数不超过 10 个，不要为了凑数而拆分。\n"
    "4. 只对**不可逆或对外**的动作（对外发布/发送、发邮件、花钱、删数据、写外部系统）"
    "把 requires_approval 设为 true；查询、分析、写作等步骤不要设置它。\n"
    "5. 只通过 submit_plan 提交，不要用普通文本回答。"
)


class PlannerAgent:
    """把用户目标拆解成 DAG 的规划者 Agent。

    契约：
    - plan(goal) 返回一个已经过 plan_to_dag 校验的 DAG；
    - 最多尝试 self.max_attempts 次；每次校验失败都把错误信息喂回给 LLM 让它自己修正；
    - 用尽次数仍失败，抛 PlannerError，并把最后一次错误存进 self.last_error。
    """

    def __init__(self, llm_client, model: str = "deepseek-v4-flash",
                 max_attempts: int = 3, max_nodes: int = 10):
        self.llm_client = llm_client
        self.model = model
        self.max_attempts = max_attempts
        self.max_nodes = max_nodes
        self.last_error: str | None = None

    def _find_submit_plan_call(self, message: Any) -> Any | None:
        """在 LLM 返回的 message.tool_calls 里找名为 submit_plan 的那个调用。"""
        for tool_call in message.tool_calls or []:
            if tool_call.function.name == "submit_plan":
                return tool_call
        return None

    def plan(self, goal: str) -> DAG:
        """把目标拆成 DAG；校验失败就把错误喂回给 LLM 重试，最多 max_attempts 次。"""
        messages = [{"role": "user", "content": goal}]
        self.last_error = None
        for _ in range(self.max_attempts):
            response = self.llm_client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": PLANNER_SYSTEM_PROMPT}] + messages,
                tools=[SUBMIT_PLAN_TOOL],
                # 踩坑记录：DeepSeek 思考模式（deepseek-v4-flash）返回
                # 400 "Thinking mode does not support this tool_choice"，
                # 即不支持 {"type":"function","function":{"name":...}} 这种强制指定。
                # 只能退到 "auto"：靠系统提示词要求它调用 + 没调用时用反馈拉回来。
                tool_choice="auto",
                max_tokens=2048,
            )
            message = response.choices[0].message
            tool_call = self._find_submit_plan_call(message)
            if tool_call is not None:
                try:
                    return plan_to_dag(extract_plan_from_tool_call(tool_call), self.max_nodes)
                except PlannerError as error:
                    feedback = f"你的计划不合法：{error}。请修正后重新调用 submit_plan。"
            else:
                feedback = "你没有调用 submit_plan 工具，请只通过 submit_plan 提交计划。"
            self.last_error = feedback
            messages.append(message)
            if tool_call is not None:
                # 工具调用必须配一条 role="tool" 的反馈，否则 API 报错（协议要求配对）
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": feedback})
            else:
                messages.append({"role": "user", "content": feedback})
        raise PlannerError(f"Planner 连续 {self.max_attempts} 次没有给出合法计划：{self.last_error}")
