from typing import List, Dict, Any
import json
from app.tools import TOOLS, tools as TOOL_SCHEMAS


class AgentFCExecutor:
    """Function Calling 版 Agent 执行器"""

    def __init__(self, llm_client, model: str = "deepseek-v4-flash", max_iterations: int = 5):
        self.llm_client = llm_client
        self.model = model
        self.max_iterations = max_iterations
        self.trajectory: List[Dict[str, Any]] = []

    def _build_system_prompt(self) -> str:
        """系统提示词（比文本解析版简单，因为工具描述已通过 tools 参数传递）"""
        return (
            "你是一个智能助手，可以调用工具来回答问题。"
            "当需要工具时直接调用，不要输出 Thought/Action 文本格式。"
            "当你有足够信息时，直接给出最终答案。"
        )

    def _call_tool(self, tool_name: str, tool_args: dict) -> str:
        """执行工具调用

        Args:
            tool_name: 工具名
            tool_args: 参数字典，如 {"query": "北京"}

        Returns:
            工具执行结果字符串
        """
        if tool_name not in TOOLS:
            return f"错误：工具 '{tool_name}' 不存在。可用工具：{list(TOOLS.keys())}"

        try:
            # **tool_args 把字典展开成关键字参数
            # 如 search(**{"query": "北京"}) → search(query="北京")
            result = TOOLS[tool_name](**tool_args)
            return result
        except Exception as e:
            return f"工具执行出错: {str(e)}"

    def run(self, user_input: str) -> Dict[str, Any]:
        """运行 Function Calling 版 Agent 循环"""
        system_prompt = self._build_system_prompt()
        messages = [{"role": "user", "content": user_input}]

        for iteration in range(self.max_iterations):
            # 调用 LLM，传入 tools 参数
            response = self.llm_client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system_prompt}] + messages,
                tools=TOOL_SCHEMAS,
                max_tokens=1024,
            )
            message = response.choices[0].message

            # 情况 1：LLM 决定调用工具
            if message.tool_calls:
                # 先把 LLM 的这条消息（含 tool_calls）追加进对话
                messages.append(message)

                # 遍历每个 tool_call（可能一次调多个）
                for tool_call in message.tool_calls:
                    tool_name = tool_call.function.name
                    # arguments 是 JSON 字符串，需要解析
                    tool_args = json.loads(tool_call.function.arguments)

                    # 执行工具
                    observation = self._call_tool(tool_name, tool_args)

                    # 记录到 trajectory
                    self.trajectory.append({
                        "iteration": iteration + 1,
                        "type": "tool_call",
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                        "observation": observation,
                    })

                    # 把工具结果作为 role="tool" 消息回传给 LLM
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,  # 必须配对
                        "content": observation,
                    })

                # 继续下一轮循环
                continue

            # 情况 2：LLM 直接回答（不调工具）
            if message.content:
                self.trajectory.append({
                    "iteration": iteration + 1,
                    "type": "final_answer",
                    "answer": message.content,
                })
                return {
                    "success": True,
                    "answer": message.content,
                    "trajectory": self.trajectory,
                    "iterations": iteration + 1,
                }

        # 达到最大迭代次数
        return {
            "success": False,
            "answer": "达到最大迭代次数，未能完成任务",
            "trajectory": self.trajectory,
            "iterations": self.max_iterations,
        }
