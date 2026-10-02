"""Agent 角色档案（阶段 5-E）：按 `agent_type` 给执行者不同的 system prompt 和工具集。

为什么不让所有 Agent 都用全部工具？（阶段 5 思考题 23，面试高频）
1. 工具越多，LLM 选错工具的概率越高（选择过载）；
2. 角色专注度：writer 若拿到 search，就可能忍不住去搜新资料，偏离
   "基于上游结果写作"的职责；
3. 权限最小化，和安全原则一致；
4. 工具 schema 本身占 token，白花钱。

Planner 在拆解 DAG 时会给每个节点标 `agent_type`（researcher / analyst / writer），
编排器把节点派发成子任务后，worker 按这个类型给执行者装上对应的"人设 + 工具"。
"""

from dataclasses import dataclass

from app.tools import TOOLS


DEFAULT_AGENT_TYPE = "researcher"


@dataclass(frozen=True)
class AgentProfile:
    """一个角色的完整设定。"""

    name: str
    system_prompt: str
    tools: tuple[str, ...]  # 工具名，必须是 app.tools.TOOLS 里的键；空元组表示不给工具


AGENT_PROFILES: dict[str, AgentProfile] = {
    # 示例（已给出）：调研员——只给 search，职责是搜集和核实信息
    "researcher": AgentProfile(
        name="researcher",
        system_prompt=(
            "你是一名调研员，擅长搜集和核实信息。"
            "请使用 search 工具查找事实性信息，并在回答中说明信息来源与不确定性。"
            "不要编造数据；查不到就明确说查不到。"
        ),
        tools=("search",),
    ),
    "analyst": AgentProfile(
        name="analyst",
        system_prompt=(
            "你是一名分析师，擅长基于已有数据做量化对比、推算和趋势判断。"
            "优先使用上游提供的材料；需要精确计算时调用 calculator。"
            "结论要给出依据，不确定的地方要标注。"
        ),
        tools=("calculator", "search"),
    ),
    "writer": AgentProfile(
        name="writer",
        system_prompt=(
            "你是一名报告撰写者。请把上游给出的结论组织成结构清晰、语言专业的报告，"
            "只使用上游提供的事实和数字，不要自行引入新数据，也不要调用任何工具。"
        ),
        tools=(),
    ),
}


def get_profile(agent_type: str | None) -> AgentProfile:
    """按 agent_type 取角色档案；未知或缺失时回退到默认角色（不能让调度因此崩掉）。"""
    if agent_type and agent_type in AGENT_PROFILES:
        return AGENT_PROFILES[agent_type]
    return AGENT_PROFILES[DEFAULT_AGENT_TYPE]
