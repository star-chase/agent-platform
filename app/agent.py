from typing import List, Dict, Any, Optional
from app.tools import TOOLS, get_tool_descriptions
import re


class AgentExecutor:
    """Agent 执行器：实现 ReAct 循环"""

    def __init__(self, llm_client, model: str = "deepseek-chat", max_iterations: int = 5):
        """
        Args:
            llm_client: OpenAI 兼容客户端实例（用 openai.OpenAI 创建，base_url 指向 DeepSeek）
            model: 模型名，如 "deepseek-chat"
            max_iterations: 最大循环次数，防止无限循环
        """
        self.llm_client = llm_client
        self.model = model
        self.max_iterations = max_iterations
        self.trajectory: List[Dict[str, Any]] = []  # 执行轨迹

    def _build_system_prompt(self) -> str:
        """构建系统提示词"""
        return f"""你是一个智能助手，可以调用工具来回答问题。

可用工具：
{get_tool_descriptions()}

你必须按照以下格式思考和行动：

Thought: [你对当前情况的分析和下一步计划]
Action: [工具名称，必须是上面列出的工具之一]
Action Input: [工具的输入参数]

观察工具执行结果后，你可以继续思考和行动，或者给出最终答案。

当你有足够信息回答用户问题时，必须输出：
Thought: 我现在可以回答用户的问题了
Final Answer: [你的最终答案]

重要提示：
- 每次只能调用一个工具
- Action 必须是可用工具列表中的名称
- 不要编造工具执行结果，等待真实的 Observation
"""

    def _parse_llm_response(self, response_text: str) -> Dict[str, Any]:
        """
        解析 LLM 的响应文本，提取 Thought/Action/Action Input/Final Answer

        Args:
            response_text: LLM 返回的原始文本

        Returns:
            解析后的字典，包含 type（"action" 或 "final_answer"）和对应字段
        """
        # TODO 1: 使用正则表达式提取 Thought
        # 提示：寻找 "Thought: " 后面的内容，直到遇到换行
        thought_match = re.search(r"Thought:\s*(.+?)(?:\n|$)", response_text, re.DOTALL)
        thought = thought_match.group(1).strip() if thought_match else ""

        # TODO 2: 检查是否是 Final Answer
        # 提示：查找 "Final Answer:" 关键词
        if "Final Answer:" in response_text:
            final_answer_match = re.search(r"Final Answer:\s*(.+)", response_text, re.DOTALL)
            final_answer = final_answer_match.group(1).strip() if final_answer_match else ""
            return {
                "type": "final_answer",
                "thought": thought,
                "final_answer": final_answer
            }

        # TODO 3: 提取 Action 和 Action Input
        # 提示：使用类似的正则表达式
        action_match = re.search(r"Action:\s*(.+?)(?:\n|$)",response_text,re.DOTALL)
        action_input_match = re.search(r"Action Input:\s*(.+?)(?:\n|$)",response_text,re.DOTALL)

        if action_match and action_input_match:
            return {
                "type": "action",
                "thought": thought,
                "action": action_match.group(1).strip(),
                "action_input": action_input_match.group(1).strip()
            }

        # 解析失败
        return {
            "type": "error",
            "message": "无法解析 LLM 响应格式",
            "raw_response": response_text
        }

    def _call_tool(self, tool_name: str, tool_input: str) -> str:
        """
        执行工具调用

        Args:
            tool_name: 工具名称
            tool_input: 工具输入

        Returns:
            工具执行结果
        """
        # TODO 4: 检查工具是否存在
        # 提示：在 TOOLS 字典中查找
        if tool_name not in TOOLS: 
            return f"错误：工具 '{tool_name}' 不存在。可用工具：{list(TOOLS.keys())}"


        # TODO 5: 调用工具函数并返回结果
        # 提示：TOOLS[tool_name](tool_input)
        return TOOLS[tool_name](tool_input)


    def run(self, user_input: str) -> Dict[str, Any]:
        """
        运行 Agent 循环

        Args:
            user_input: 用户的问题

        Returns:
            包含 answer、trajectory、iterations 的结果字典
        """
        system_prompt = self._build_system_prompt()

        # 对话历史（用于多轮对话）
        messages = [
            {"role": "user", "content": user_input}
        ]

        for iteration in range(self.max_iterations):
            # TODO 6: 调用 LLM（DeepSeek 走 OpenAI 兼容接口）
            client = self.llm_client
            response = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system_prompt}] + messages,
                max_tokens=1024,
                stop=["Observation:"],          # 防止 LLM 幻觉自答（呼应思考题1）
            )
            llm_response_text = response.choices[0].message.content



            # TODO 7: 解析 LLM 响应
            parsed = self._parse_llm_response(llm_response_text)

            # TODO 8: 如果是 Final Answer，结束循环
            if parsed["type"] == "final_answer":
                self.trajectory.append({
                    "iteration": iteration + 1,
                    "thought": parsed["thought"],
                    "type": "final_answer",
                    "final_answer": parsed["final_answer"]
                })
                return {
                    "success": True,
                    "answer": parsed["final_answer"],
                    "trajectory": self.trajectory,
                    "iterations": iteration + 1
                }

            
            # TODO 9: 如果是 Action，执行工具调用
            if parsed["type"] == "action":
                
                observation = self._call_tool(parsed["action"],parsed["action_input"])  # 调用 self._call_tool()
                last = self.trajectory[-1] if self.trajectory else None
                if last and last.get("action") and last["action"] == parsed["action"] and last.get("action_input") == parsed["action_input"]:
                        observation = observation +"\n[系统提示] 你刚才已用相同参数调用过此工具，结果一致。请换个思路或直接给出答案。"

                # TODO 10: 记录到 trajectory
                self.trajectory.append({
                    "iteration": iteration + 1,
                    "thought": parsed["thought"],
                    "action": parsed["action"],
                    "action_input": parsed["action_input"],
                    "observation": observation
                })

                # TODO 11: 将 LLM 响应和工具结果追加到对话历史
                messages.append({"role": "assistant", "content": llm_response_text})          
                messages.append({"role": "user", "content": f"Observation: {observation}"})

            # TODO 12: 如果解析错误，如何处理？
            if parsed["type"] == "error":
                # 把格式错误 + 纠错要求喂回去，让 LLM 重新输出
                messages.append({"role": "assistant", "content": llm_response_text})
                messages.append({"role": "user", "content": (
                    f"你上一次的输出格式不对，无法解析（原因：{parsed['message']}）。"
                    "请严格按以下格式重新输出，不要多说："
                    "\nThought: ...\nAction: 工具名\nAction Input: 参数"
                )})


                self.trajectory.append({
                    "iteration": iteration + 1,
                    "type": "error",
                    "raw_response": response_text
                })
                continue


        # 达到最大迭代次数
        return {
            "success": False,
            "answer": "达到最大迭代次数，未能完成任务",
            "trajectory": self.trajectory,
            "iterations": self.max_iterations
        }
