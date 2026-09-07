from typing import List, Dict, Any
import json
import sqlite3
import uuid
from datetime import datetime
from app.tools import TOOLS, tools as TOOL_SCHEMAS


class AgentFCPersistentExecutor:
    """Function Calling 版 Agent 执行器 + 状态持久化"""

    def __init__(self, llm_client, model: str = "deepseek-v4-flash", max_iterations: int = 5, db_path: str = "agent.db"):
        self.llm_client = llm_client
        self.model = model
        self.max_iterations = max_iterations
        self.db_path = db_path
        self.task_id = None
        self.trajectory: List[Dict[str, Any]] = []

    def _get_connection(self):
        """获取数据库连接"""
        return sqlite3.connect(self.db_path)

    def _build_system_prompt(self) -> str:
        """系统提示词"""
        return (
            "你是一个智能助手，可以调用工具来回答问题。"
            "当需要工具时直接调用，不要输出 Thought/Action 文本格式。"
            "当你有足够信息时，直接给出最终答案。"
        )

    def _call_tool(self, tool_name: str, tool_args: dict) -> str:
        """执行工具调用"""
        if tool_name not in TOOLS:
            return f"错误：工具 '{tool_name}' 不存在。可用工具：{list(TOOLS.keys())}"

        try:
            result = TOOLS[tool_name](**tool_args)
            return result
        except Exception as e:
            return f"工具执行出错: {str(e)}"

    def _create_task(self, question: str) -> str:
        """创建任务记录并返回 task_id"""
        task_id = str(uuid.uuid4())
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO tasks (id, question, status, max_iterations)
            VALUES (?, ?, 'RUNNING', ?)
        """, (task_id, question, self.max_iterations))

        conn.commit()
        conn.close()
        return task_id

    def _update_task_status(self, status: str, answer: str = None):
        """更新任务状态"""
        conn = self._get_connection()
        cursor = conn.cursor()

        if answer:
            cursor.execute("""
                UPDATE tasks
                SET status = ?, answer = ?, updated_at = datetime('now')
                WHERE id = ?
            """, (status, answer, self.task_id))
        else:
            cursor.execute("""
                UPDATE tasks
                SET status = ?, updated_at = datetime('now')
                WHERE id = ?
            """, (status, self.task_id))

        conn.commit()
        conn.close()

    def _save_step(self, iteration: int, step_type: str, tool_name: str = None,
                   tool_args: dict = None, observation: str = None, answer: str = None,
                   llm_message = None):
        """保存步骤到数据库

        Args:
            llm_message: LLM 的原始 message 对象（含 tool_calls、reasoning_content 等厂商字段）
        """
        step_id = str(uuid.uuid4())
        conn = self._get_connection()
        cursor = conn.cursor()

        # 把 LLM message 序列化成 JSON 字符串存储
        llm_message_json = None
        if llm_message:
            # 转成字典再序列化（message 是 Pydantic 对象，需要先转 dict）
            llm_message_json = json.dumps(llm_message.model_dump() if hasattr(llm_message, 'model_dump') else llm_message)

        cursor.execute("""
            INSERT INTO steps (id, task_id, iteration, type, tool_name, tool_args, observation, finished_at, llm_message_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
        """, (
            step_id,
            self.task_id,
            iteration,
            step_type,
            tool_name,
            json.dumps(tool_args) if tool_args else None,
            observation or answer,
            llm_message_json
        ))

        conn.commit()
        conn.close()

    def run(self, user_input: str) -> Dict[str, Any]:
        """运行 Function Calling 版 Agent 循环（带持久化）"""
        # 1. 创建任务记录
        self.task_id = self._create_task(user_input)

        system_prompt = self._build_system_prompt()
        messages = [{"role": "user", "content": user_input}]

        try:
            for iteration in range(self.max_iterations):
                # 调用 LLM
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

                        # 2. 立即保存步骤到数据库（存储 LLM 原始 message）
                        self._save_step(
                            iteration=iteration + 1,
                            step_type="tool_call",
                            tool_name=tool_name,
                            tool_args=tool_args,
                            observation=observation,
                            llm_message=message  # ← 新增：存储 LLM 原始响应
                        )

                        # 记录到内存 trajectory（用于返回）
                        self.trajectory.append({
                            "iteration": iteration + 1,
                            "type": "tool_call",
                            "tool_name": tool_name,
                            "tool_args": tool_args,
                            "observation": observation,
                        })

                        # 回传工具结果
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": observation,
                        })

                    continue

                # 情况 2：LLM 直接回答
                if message.content:
                    # 3. 保存最终答案（含 LLM message）
                    self._save_step(
                        iteration=iteration + 1,
                        step_type="final_answer",
                        answer=message.content,
                        llm_message=message  # ← 新增：存储 LLM 原始响应
                    )

                    self.trajectory.append({
                        "iteration": iteration + 1,
                        "type": "final_answer",
                        "answer": message.content,
                    })

                    # 4. 更新任务状态为 COMPLETED
                    self._update_task_status("COMPLETED", message.content)

                    return {
                        "success": True,
                        "task_id": self.task_id,
                        "answer": message.content,
                        "trajectory": self.trajectory,
                        "iterations": iteration + 1,
                    }

            # 达到最大迭代次数
            self._update_task_status("FAILED", "达到最大迭代次数，未能完成任务")

            return {
                "success": False,
                "task_id": self.task_id,
                "answer": "达到最大迭代次数，未能完成任务",
                "trajectory": self.trajectory,
                "iterations": self.max_iterations,
            }

        except Exception as e:
            # 5. 异常时更新任务状态为 FAILED
            self._update_task_status("FAILED", f"执行出错: {str(e)}")
            raise

    def resume(self, task_id: str) -> Dict[str, Any]:
        """断点续跑：从数据库恢复任务并继续执行（方案 A：重建完整 messages）

        Args:
            task_id: 要恢复的任务 ID

        Returns:
            执行结果
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # 1. 读取任务信息
        cursor.execute("SELECT question, status, max_iterations FROM tasks WHERE id = ?", (task_id,))
        task_row = cursor.fetchone()
        if not task_row:
            conn.close()
            return {"success": False, "error": f"任务 {task_id} 不存在"}

        question, status, max_iterations = task_row

        if status == "COMPLETED":
            conn.close()
            return {"success": False, "error": "任务已完成，无需续跑"}

        # 2. 读取已完成的 steps（包含 llm_message_json）
        cursor.execute("""
            SELECT iteration, type, tool_name, tool_args, observation, llm_message_json
            FROM steps
            WHERE task_id = ?
            ORDER BY iteration
        """, (task_id,))
        completed_steps = cursor.fetchall()
        conn.close()

        # 3. 重建 messages 对话历史（从数据库里的 LLM 原始响应）
        self.task_id = task_id
        self.max_iterations = max_iterations

        system_prompt = self._build_system_prompt()
        messages = [{"role": "user", "content": question}]

        # 记录真正成功恢复的 iteration，不能用 completed_steps 的长度代替。
        restored_iterations = []

        for step in completed_steps:
            iteration, step_type, tool_name, tool_args_json, observation, llm_message_json = step

            # 缺少原始 assistant 消息时，无法安全重建 tool_call/tool_result 配对。
            if step_type == "tool_call" and not llm_message_json:
                return {
                    "success": False,
                    "error": (
                        f"任务无法续跑：第 {iteration} 步缺少 llm_message_json，"
                        "这是迁移前的旧数据，请重新执行任务或补齐历史消息"
                    ),
                }

            if step_type == "tool_call" and llm_message_json:
                # 从数据库恢复 LLM 的原始 message 对象
                llm_message_dict = json.loads(llm_message_json)

                # 重建 assistant 消息（完整的 LLM 响应，含 reasoning_content 等字段）
                messages.append({
                    "role": "assistant",
                    "content": llm_message_dict.get("content"),
                    "tool_calls": llm_message_dict.get("tool_calls")
                })

                # 重建 tool 消息（取第一个 tool_call 的 id）
                tool_call_id = llm_message_dict["tool_calls"][0]["id"] if llm_message_dict.get("tool_calls") else f"call_{task_id}_{iteration}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": observation
                })

                # 恢复 trajectory
                tool_args = json.loads(tool_args_json) if tool_args_json else {}
                self.trajectory.append({
                    "iteration": iteration,
                    "type": step_type,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "observation": observation
                })

                restored_iterations.append(iteration)

        # 4. 从下一个 iteration 继续执行
        # start_iteration 是 range() 使用的零基起点。
        start_iteration = max(restored_iterations, default=0)
        print(f"📍 从第 {start_iteration + 1} 轮继续（已完成 {len(completed_steps)} 步，messages 已重建）...")

        # 更新任务状态为 RUNNING
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE tasks SET status = 'RUNNING' WHERE id = ?", (task_id,))
        conn.commit()
        conn.close()

        # 5. 继续循环
        try:
            for iteration in range(start_iteration, self.max_iterations):
                response = self.llm_client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": system_prompt}] + messages,
                    tools=TOOL_SCHEMAS,
                    max_tokens=1024,
                )
                message = response.choices[0].message

                if message.tool_calls:
                    messages.append(message)

                    for tool_call in message.tool_calls:
                        tool_name = tool_call.function.name
                        tool_args = json.loads(tool_call.function.arguments)
                        observation = self._call_tool(tool_name, tool_args)

                        self._save_step(
                            iteration=iteration + 1,
                            step_type="tool_call",
                            tool_name=tool_name,
                            tool_args=tool_args,
                            observation=observation,
                            llm_message=message
                        )

                        self.trajectory.append({
                            "iteration": iteration + 1,
                            "type": "tool_call",
                            "tool_name": tool_name,
                            "tool_args": tool_args,
                            "observation": observation,
                        })

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": observation,
                        })

                    continue

                if message.content:
                    self._save_step(
                        iteration=iteration + 1,
                        step_type="final_answer",
                        answer=message.content,
                        llm_message=message
                    )

                    self.trajectory.append({
                        "iteration": iteration + 1,
                        "type": "final_answer",
                        "answer": message.content,
                    })

                    self._update_task_status("COMPLETED", message.content)

                    return {
                        "success": True,
                        "task_id": self.task_id,
                        "answer": message.content,
                        "trajectory": self.trajectory,
                        "iterations": iteration + 1,
                        "resumed_from": start_iteration
                    }

            self._update_task_status("FAILED", "达到最大迭代次数")
            return {
                "success": False,
                "task_id": self.task_id,
                "answer": "达到最大迭代次数",
                "trajectory": self.trajectory,
                "iterations": self.max_iterations,
            }

        except Exception as e:
            self._update_task_status("FAILED", f"执行出错: {str(e)}")
            raise
