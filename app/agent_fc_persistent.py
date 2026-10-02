from typing import List, Dict, Any, Callable
import json
import logging
import sqlite3
import uuid
from datetime import datetime
from app.db import connect as connect_db, utc_now_ms
from app.cost import task_budget_status
from app.progress_guard import NO_PROGRESS_HINT, NoProgressDetector
from app.tools import REQUEST_APPROVAL_TOOL, TOOLS, tools as TOOL_SCHEMAS


logger = logging.getLogger(__name__)


class AgentFCPersistentExecutor:
    """Function Calling 版 Agent 执行器 + 状态持久化"""

    def __init__(
        self,
        llm_client,
        model: str = "deepseek-v4-flash",
        max_iterations: int = 5,
        db_path: str = "agent.db",
        on_step: Callable[[dict], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        system_prompt: str | None = None,
        tool_names: tuple[str, ...] | None = None,
        request_approval: Callable[[str, str], str] | None = None,
    ):
        self.llm_client = llm_client
        self.model = model
        self.max_iterations = max_iterations
        self.db_path = db_path
        self.task_id = None
        self.trajectory: List[Dict[str, Any]] = []
        self.on_step = on_step
        self.should_stop = should_stop
        # 阶段 5-E：角色档案注入。system_prompt=None 用默认提示词；
        # tool_names=None 用全部工具，() 表示这个角色不给任何工具。
        self.system_prompt = system_prompt
        self.tool_names = tool_names
        # 阶段 6-D：动态审批。Agent 调用 request_human_approval 时用它建审批记录
        # （签名：task_id, reason -> approval_id）。不注入时，一旦请求审批就报错
        # （fail closed：宁可失败，也不能假装批准过）。
        self.request_approval = request_approval
        # 无进展检测：连续多轮拿不到新信息就强制收尾（阶段 1 任务 1-F 的落地）
        self.progress_detector = NoProgressDetector()

    def _get_connection(self):
        """获取数据库连接（统一走 app.db，带 busy_timeout + WAL，避免并发写锁冲突）"""
        return connect_db(self.db_path)

    # 阶段 6-D：审批规则对**所有角色**生效（和"控制面工具不参与角色裁剪"配套）。
    # 只把工具挂上去、不在提示词里说明什么时候用它，LLM 基本不会主动调用。
    APPROVAL_RULE = (
        "\n\n【人工审批规则】在执行不可逆或对外的动作之前"
        "（对外发布/发送、发邮件或消息、花钱、删除数据、写入外部系统），"
        "必须先调用 request_human_approval 请求人类批准；"
        "批准后你会收到人类的意见，请按意见继续。"
        "查询、分析、写作这类只读步骤不要调用它。"
    )

    def _build_system_prompt(self) -> str:
        """系统提示词：角色档案注入的 self.system_prompt 优先，否则用默认提示词。

        无论哪种情况都追加审批规则 —— 它是控制面约束，不属于角色设定。
        """
        base = self.system_prompt or (
            "你是一个智能助手，可以调用工具来回答问题。"
            "当需要工具时直接调用，不要输出 Thought/Action 文本格式。"
            "当你有足够信息时，直接给出最终答案。"
        )
        return base + self.APPROVAL_RULE

    def _tools_kwargs(self) -> dict:
        """构造传给 LLM 的工具参数（阶段 5-E：按角色裁剪工具集）。

        - self.tool_names is None → 全部工具（兼容阶段 1-4 的老调用）；
        - 按名字过滤后还有工具 → {"tools": [...]}；
        - 过滤后为空（如 writer 角色）→ 返回空字典，**完全省略 tools 参数**。

        为什么"没有工具"要省略、而不是传 `tools=[]`（2026-09-17 实测验过）：
        - OpenAI 官方接口会 400：`Invalid 'tools': expected an array with at least
          one element, but received an empty array`；
        - **DeepSeek 兼容层实测是容忍的**（传 tools=[] 照样返回成功），
          但这是未文档化的宽容行为，不能当成契约来依赖；
        - 所以省略该参数是唯一跨厂商都安全的写法——按最严格的接口写代码。
        """
        # 阶段 6-D：控制面工具（请求人工审批）和业务工具分开算 ——
        # 它是"所有角色的通用能力"，不该被角色档案的工具裁剪掉：
        # 恰恰是 writer 这种"要对外发布"的角色最需要它。
        if self.tool_names is None:
            selected = list(TOOL_SCHEMAS)
        else:
            selected = [
                schema for schema in TOOL_SCHEMAS
                if schema["function"]["name"] in self.tool_names
            ]
        return {"tools": selected + [REQUEST_APPROVAL_TOOL]}

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
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO tasks (id, question, status, max_iterations)
                VALUES (?, ?, 'RUNNING', ?)
            """, (task_id, question, self.max_iterations))
            conn.commit()
        finally:
            # 必须 try/finally 关闭连接：异常路径漏关会泄漏写锁，
            # 让后续写操作一直等到 busy_timeout 超时（2026-09-17 实测卡 30 秒）。
            conn.close()
        return task_id

    def _update_task_status(self, status: str, answer: str = None):
        """更新任务状态"""
        conn = self._get_connection()
        try:
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
        finally:
            conn.close()

    def _get_approval_status(self, approval_id: str):
        """读一条审批记录，返回 (status, comment)。

        抽成一个方法而不是在 resume() 里直接写 SQL：审批决定的读取**只有一处实现**，
        将来换存储（比如阶段 8 换 PostgreSQL）只改这里。
        找不到记录时返回 (None, None)，由调用方决定怎么处理。
        """
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT status, comment FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None, None
        return row[0], row[1]

    @staticmethod
    def _extract_usage(response) -> tuple[int, int]:
        """从 LLM 响应里取出 (input_tokens, output_tokens)（阶段 7-A）。

        为什么要写成 getattr 兜底：不同厂商/不同 SDK 版本字段名不一样
        （OpenAI 是 prompt_tokens / completion_tokens），假客户端也未必有这个属性。
        **计量代码不能因为"拿不到用量"就把主流程搞崩** —— 拿不到就记 0。
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            return 0, 0
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
        return int(input_tokens), int(output_tokens)

    def _save_step(self, iteration: int, step_type: str, tool_name: str = None,
                   tool_args: dict = None, observation: str = None, answer: str = None,
                   llm_message = None, input_tokens: int = 0, output_tokens: int = 0,
                   started_at: str = None):
        """保存步骤到数据库

        Args:
            llm_message: LLM 的原始 message 对象（含 tool_calls、reasoning_content 等厂商字段）
            input_tokens / output_tokens: 这一步那次 LLM 调用花的 token（阶段 7-A）
            started_at: **这次 LLM 调用开始**的时刻（阶段 7-C）。

                以前这里靠列默认值 datetime('now')，而 finished_at 也在 INSERT 时取 now ——
                两个时间戳几乎相同，于是 `finished_at - started_at` 恒为 0，
                这两列**等于没在用**（追踪功能一直缺的就是这一半数据）。
                现在：调用前在 run()/resume() 里取一次时间传进来，
                finished_at 取写库时刻 —— 两者之差就是这次 LLM 调用的真实延迟。
        """
        step_id = str(uuid.uuid4())
        conn = self._get_connection()
        try:
            cursor = conn.cursor()

            # 把 LLM message 序列化成 JSON 字符串存储
            llm_message_json = None
            if llm_message:
                # 转成字典再序列化（message 是 Pydantic 对象，需要先转 dict）
                llm_message_json = json.dumps(llm_message.model_dump() if hasattr(llm_message, 'model_dump') else llm_message)

            cursor.execute("""
                INSERT INTO steps (id, task_id, iteration, type, tool_name, tool_args, observation, started_at, finished_at, llm_message_json, input_tokens, output_tokens)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                step_id,
                self.task_id,
                iteration,
                step_type,
                tool_name,
                json.dumps(tool_args) if tool_args else None,
                observation or answer,
                started_at or utc_now_ms(),
                utc_now_ms(),
                llm_message_json,
                input_tokens,
                output_tokens,
            ))

            conn.commit()
        finally:
            conn.close()
        # 每存一步打一条 step_saved。task_id **不用手写** —— 上下文里有。
        # ⚠️ 必须用 extra={...}，不能写成 logger.info("step_saved", iteration=3)：
        #    logging 的关键字参数只认 exc_info/stack_info/stacklevel/extra，
        #    多一个就是 TypeError（第一次用结构化日志的人都会踩）。
        # 这里也可能**没有**上下文（离线单测直接调执行器）：
        # 少几个字段是正常的，JsonFormatter 不会因此报错。
        step_log_extra = {
            "step_type": step_type,
            "tool_name": tool_name,
            "iteration": iteration,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        logger.info("step_saved", extra=step_log_extra)

    def _notify_step(self, step: dict) -> None:
        """向外部通知一步执行完成；具体通知方式由调用方注入。"""
        if self.on_step:
            self.on_step(step)

    def _is_stop_requested(self) -> bool:
        """检查外部是否请求在安全点停止当前任务。"""
        return bool(self.should_stop and self.should_stop())

    def _budget_guard(self, iteration: int) -> Dict[str, Any] | None:
        """每一次 LLM 调用**之前**的预算闸门（阶段 7-A-2）。

        返回 None = 放行，继续调 LLM；返回 dict = 已终止，调用方直接把它 return 出去。

        这是思考题 31 的落地：**先检查、后消费**。
        放在调用之后检查，钱已经花掉了，那叫事后追认，不叫闸门。

        谁来调用它：run() 和 resume() 的循环各一处，都紧贴 create() 之前。
        **闸门只装在一个入口上就不算闸门** —— 审批批准后走的是 resume，
        那条路上的花费一样要受预算约束。

        超支时做两件事：
          1. self._update_task_status("BUDGET_EXCEEDED", 一句人能看懂的理由)
             用 BUDGET_EXCEEDED 而不是 FAILED：调度器一看就知道这是**确定性失败**
             （重试必然再次超支），不能白耗节点级重试的额度。
          2. 返回 {"success": False, "status": "BUDGET_EXCEEDED", "budget_exceeded": True,
                   "task_id": self.task_id, "answer": 理由,
                   "trajectory": self.trajectory, "iterations": iteration}
             形状对齐"暂停等审批"的返回值：调用方靠 status 一个字段就能判断原因。

        数据从哪来：app.cost.task_budget_status(self.db_path, self.task_id, self.model)。
        注意它每次都会查库 —— 预算要按**最新**的累计花费判，缓存一份在内存里就会漏判。
        """
        if self.task_id is None:
            return None
        budget_status = task_budget_status(self.db_path, self.task_id, self.model)
        exceeded = budget_status.get("exceeded", False)
        if exceeded:
            reason = (
                f"预算超支：已花 {budget_status.get('spent_usd')} USD，"
                f"预算 {budget_status.get('budget_usd')} USD，"
                f"剩余 {budget_status.get('remaining_usd')} USD"
            )
            self._update_task_status("BUDGET_EXCEEDED", reason)
            # 用 WARNING 而不是 ERROR：这不是程序出错，是**预设的闸门正常生效**。
            # 日志里带上 spent/budget/remaining，事后能直接算超了多少，不用再查库。
            logger.warning("budget_exceeded", extra={
                "spent_usd": budget_status.get("spent_usd"),
                "budget_usd": budget_status.get("budget_usd"),
                "remaining_usd": budget_status.get("remaining_usd"),
            })
            return {
                "success": False,
                "status": "BUDGET_EXCEEDED",
                "budget_exceeded": True,
                "task_id": self.task_id,
                "answer": reason,
                "trajectory": self.trajectory,
                "iterations": iteration,
            }
        return None
   

    def run(self, user_input: str, task_id: str | None = None) -> Dict[str, Any]:
        """运行 Function Calling 版 Agent 循环（带持久化）"""
        if task_id is None:
            self.task_id = self._create_task(user_input)
        else:
            self.task_id = task_id
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE tasks SET status = 'RUNNING', updated_at = datetime('now') "
                    "WHERE id = ? AND status = 'PENDING'",
                    (task_id,),
                )
                conn.commit()
            finally:
                conn.close()

        system_prompt = self._build_system_prompt()
        messages = [{"role": "user", "content": user_input}]

        try:
            for iteration in range(self.max_iterations):
                if self._is_stop_requested():
                    return {
                        "success": False,
                        "task_id": self.task_id,
                        "answer": "任务已取消",
                        "trajectory": self.trajectory,
                        "iterations": iteration,
                        "cancelled": True,
                    }
                # 闸门①（7-A-2）：紧贴下面那次 create() 之前判预算，超了直接 return。
                # 放在 create() 之后等于钱已经花掉了 —— 那叫事后追认，不叫闸门。
                stopped = self._budget_guard(iteration)
                if stopped:
                    return stopped

                # 阶段 7-C：调用**之前**取时间。finished_at 在写 step 时取 ——
                # 两者之差就是这次 LLM 调用的真实延迟（追踪的原始数据）。
                call_started_at = utc_now_ms()
                response = self.llm_client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": system_prompt}] + messages,
                    max_tokens=1024,
                    **self._tools_kwargs(),
                )
                message = response.choices[0].message
                # 阶段 7-A：这次调用花了多少 token，从响应里取出来传给处理逻辑记账
                usage = self._extract_usage(response)

                # 三种情况（工具调用 / 直接回答 / 空响应）统一交给共享方法处理，
                # 这样 run() 和 resume() 不会各改一份、改一处漏一处
                # （这份复制粘贴已经坑了三次：并行工具调用、resume 配对、无进展检测）。
                # 必须传 self.task_id：run() 的入参 task_id 可能是 None
                # （调用方没传时，任务行是 _create_task 建的，id 记在 self.task_id）
                result = self._handle_llm_message(
                    message, messages, system_prompt, iteration, self.task_id, usage,
                    call_started_at,
                )
                if result is not None:
                    return result

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
            # 异常时更新任务状态为 FAILED
            self._update_task_status("FAILED", f"执行出错: {str(e)}")
            raise

    def _handle_llm_message(
        self, message: Any, messages: list, system_prompt: str, iteration: int,
        task_id: str, usage: tuple[int, int] = (0, 0), started_at: str = None,
    ) -> Dict[str, Any] | None:
        """处理一轮 LLM 响应。返回结果字典 = 任务已结束；返回 None = 继续下一轮。

        run() 和 resume() 共用这一份逻辑。此前两处各有一份复制粘贴的循环，
        结果连续出了三次问题（并行工具调用、resume 配对、无进展检测），
        所以合并到这里——以后只需要改一个地方。

        三种情况：
        1. LLM 调用工具：一轮 = 一条 step（一次响应可能并行返回多个 tool_call，
           每个都必须回传配对的 tool 结果）；顺带做无进展检测。
        2. LLM 直接回答：存 final_answer、置 COMPLETED、结束。
        3. 空响应（既无 tool_calls 也无 content）：当成一次"无进展"，
           否则会白烧迭代次数，最后被判"达到最大迭代次数"。
        """
        # usage 是**那一次 LLM 调用**的开销，而这一步正是那次调用的结果 ——
        # 记账要记在产生开销的那一步上。本方法内 3 处 _save_step 都要带上它：
        # 漏记的后果是成本少算，而"少算"比"算错"更危险（预算防线永远不触发）。

        # ---- 情况 0：Agent 主动请求人工审批（阶段 6-D 动态审批）----
        # 这一轮不按普通工具调用处理：request_human_approval 没有实现、也不该有返回值，
        # 它表达的是"停下来等人类"。所以这里存一条 approval_request step 就返回。
        # 注意**不存 llm_message、也不回传 tool_result**：留下悬空的 tool_call 会让
        # 下次 resume 重建 messages 时违反"每个 tool_call 必须有配对 tool_result"（思考题 8）。
        if message.tool_calls and message.tool_calls[0].function.name == "request_human_approval":
            tool_call = message.tool_calls[0]
            tool_args = json.loads(tool_call.function.arguments)
            reason = tool_args.get("reason", "无理由")
            proposed_action = tool_args.get("proposed_action", "无拟执行动作")

            if not self.request_approval:
                raise RuntimeError(
                    "Agent 请求人工审批，但未注入 request_approval 回调，无法继续。"
                    "请在初始化 AgentFCPersistentExecutor 时传入 request_approval 参数。"
                )

            approval_id = self.request_approval(task_id, f"{reason}（拟执行：{proposed_action}）")

            # 存一条 step，注意不要传 llm_message（否则 resume 会报错）
            self._save_step(
                iteration=iteration + 1,
                step_type="approval_request",
                tool_name="request_human_approval",
                tool_args={**tool_args, "approval_id": approval_id},
                observation="等待人工审批",
                input_tokens=usage[0],
                output_tokens=usage[1],
                started_at=started_at,
            )
            self.trajectory.append({
                "iteration": iteration + 1,
                "type": "approval_request",
                "tool_name": "request_human_approval",
                "tool_args": {**tool_args, "approval_id": approval_id},
                "observation": "等待人工审批",
            })
            self._notify_step(self.trajectory[-1])

            self._update_task_status("WAITING_APPROVAL")
            return {
                "success": False,
                "task_id": task_id,
                "status": "WAITING_APPROVAL",
                "approval_id": approval_id,
                "trajectory": self.trajectory,
                "iterations": iteration + 1,
            }

        # ---- 情况 1：调用工具 ----
        if message.tool_calls:
            messages.append(message)

            tool_names, tool_args_list, observations = [], [], []
            for tool_call in message.tool_calls:
                tool_name = tool_call.function.name
                tool_args = json.loads(tool_call.function.arguments)
                observation = self._call_tool(tool_name, tool_args)

                tool_names.append(tool_name)
                tool_args_list.append(tool_args)
                observations.append(observation)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": observation,
                })

            if len(tool_names) == 1:
                step_tool_name = tool_names[0]
                step_tool_args = tool_args_list[0]
                step_observation = observations[0]
            else:
                step_tool_name = json.dumps(tool_names, ensure_ascii=False)
                step_tool_args = tool_args_list
                step_observation = json.dumps(observations, ensure_ascii=False)

            self._save_step(
                iteration=iteration + 1,
                step_type="tool_call",
                tool_name=step_tool_name,
                tool_args=step_tool_args,
                observation=step_observation,
                # 必须存原始 assistant 消息，否则 resume() 无法重建 tool_call / tool_result 配对
                llm_message=message,
                input_tokens=usage[0],
                output_tokens=usage[1],
                started_at=started_at,
            )
            self.trajectory.append({
                "iteration": iteration + 1,
                "type": "tool_call",
                "tool_name": step_tool_name,
                "tool_args": step_tool_args,
                "observation": step_observation,
            })
            self._notify_step(self.trajectory[-1])

            if self.progress_detector.observe(observations):
                # 连续 N 轮零新信息 → 强制收尾；本轮 tool_call 已占 iteration+1
                return self._force_final_answer(system_prompt, messages, iteration + 2)
            if self.progress_detector.consecutive_no_progress > 0:
                messages.append({"role": "user", "content": NO_PROGRESS_HINT})
            return None

        # ---- 情况 2：直接回答 ----
        if message.content:
            self._save_step(
                iteration=iteration + 1,
                step_type="final_answer",
                answer=message.content,
                llm_message=message,
                input_tokens=usage[0],
                output_tokens=usage[1],
                started_at=started_at,
            )
            self.trajectory.append({
                "iteration": iteration + 1,
                "type": "final_answer",
                "answer": message.content,
            })
            self._notify_step(self.trajectory[-1])
            self._update_task_status("COMPLETED", message.content)
            return {
                "success": True,
                "task_id": self.task_id,
                "answer": message.content,
                "trajectory": self.trajectory,
                "iterations": iteration + 1,
            }

        # ---- 情况 3：空响应（模型这一轮什么都没说）----
        # observe([]) 天然算"没有新信息"（空集合的差集为空）。
        if self.progress_detector.observe([]):
            # 这一轮没写 step，所以最终答案用 iteration+1
            return self._force_final_answer(system_prompt, messages, iteration + 1)
        if self.progress_detector.consecutive_no_progress > 0:
            messages.append({"role": "user", "content": NO_PROGRESS_HINT})
        return None

    def _force_final_answer(self, system_prompt: str, messages: list, step_number: int) -> Dict[str, Any]:
        """不带 tools 再调一次 LLM，强制它输出文本答案——别再空转烧钱了。

        Args:
            step_number: 这条最终答案要占用的 step 号，由调用方决定：
                - 工具轮之后调用：本轮的 tool_call 已占用 iteration+1，所以传 iteration+2；
                - 空响应轮之后调用：那一轮没写任何 step，所以传 iteration+1。
                传错就会出现 step 号重复（撞 UNIQUE）或空洞。
        """
        call_started_at = utc_now_ms()                 # 阶段 7-C：强制收尾也是一次真调用
        response = self.llm_client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system_prompt}] + messages,
            max_tokens=1024,
        )
        message = response.choices[0].message
        answer = message.content or "（模型在无工具模式下仍未给出内容）"
        input_tokens, output_tokens = self._extract_usage(response)   # 阶段 7-A

        self._save_step(
            iteration=step_number,
            step_type="final_answer",
            answer=answer,
            llm_message=message,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            started_at=call_started_at,
        )
        self.trajectory.append({
            "iteration": step_number,
            "type": "final_answer",
            "answer": answer,
        })
        self._notify_step(self.trajectory[-1])
        self._update_task_status("COMPLETED", answer)

        return {
            "success": True,
            "task_id": self.task_id,
            "answer": answer,
            "trajectory": self.trajectory,
            "iterations": step_number,
            "forced_final_answer": True,
        }

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
        
        if status == "CANCELLED":
            conn.close()
            return {
                "success": False,
                # 带上 status，和"暂停等审批"的返回值形状保持一致：
                # 调用方拿到的每种非成功结果，都能用一个字段判断原因
                "status": "CANCELLED",
                "task_id": task_id,
                "answer": "任务已取消",
                "trajectory": [],
                "iterations": 0,
                "cancelled": True,
}

        # BUDGET_EXCEEDED 也是终态（7-A-2）：已经被预算拦下的任务不该再续跑，
        # 返回形状照上面 CANCELLED 的写法，同样带上 status。
        if status == "BUDGET_EXCEEDED":
            conn.close()
            return {
                "success": False,
                "status": "BUDGET_EXCEEDED",
                "task_id": task_id,
                "answer": "任务已被预算拦下，无法续跑",
                "trajectory": [],
                "iterations": 0,
                "budget_exceeded": True,
            }

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

            # 审批步骤：把人类的决定注入上下文（用 user 消息 —— 这次审批的 tool_call
            # 没有 tool_call_id 可配对，硬塞 tool 消息会违反配对协议）
            if step_type == "approval_request":
                tool_args = json.loads(tool_args_json) if tool_args_json else {}
                approval_id = tool_args.get("approval_id")
                if not approval_id:
                    return {
                        "success": False,
                        "error": (
                            f"任务无法续跑：第 {iteration} 步缺少 approval_id，"
                            "这是迁移前的旧数据，请重新执行任务或补齐历史消息"
                        ),
                    }
                # 读取审批记录（假设有一个函数 get_approval_status(approval_id)）
                approval_status, human_reason = self._get_approval_status(approval_id)
                if approval_status == "APPROVED":
                    messages.append({
                        "role": "user",
                        "content": f"【人工审批】已批准。人类意见：{human_reason or '（无）'}"
                    })
                elif approval_status == "REJECTED":
                    messages.append({
                        "role": "user",
                        "content": f"【人工审批】被拒绝。理由：{human_reason or '（无）'}"
                    })
                else:
                    messages.append({
                        "role": "user",
                        "content": f"【人工审批】状态未知。请检查审批记录。"
                    })
                self.trajectory.append({
                    "iteration": iteration,
                    "type": step_type,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "observation": observation
                })
                self._notify_step(self.trajectory[-1])
                restored_iterations.append(iteration)
                continue

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

                # 重建 tool 消息：必须和 tool_calls **一一配对**。
                # 并行调用时 step 里的 observation 是 JSON 数组字符串，按顺序还原；
                # 单工具调用保持原样（向后兼容老数据）。
                tool_calls = llm_message_dict.get("tool_calls") or []
                if len(tool_calls) > 1:
                    observations = json.loads(observation)
                else:
                    observations = [observation]
                if tool_calls:
                    for tool_call, tool_observation in zip(tool_calls, observations):
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.get("id"),
                            "content": tool_observation,
                        })
                else:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": f"call_{task_id}_{iteration}",
                        "content": observation,
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
                self._notify_step(self.trajectory[-1])

                restored_iterations.append(iteration)

        # 4. 从下一个 iteration 继续执行
        # start_iteration 是 range() 使用的零基起点。
        start_iteration = max(restored_iterations, default=0)
        print(f"📍 从第 {start_iteration + 1} 轮继续（已完成 {len(completed_steps)} 步，messages 已重建）...")

        # 更新任务状态为 RUNNING
        conn = self._get_connection()
        cursor = conn.cursor()
        # 阶段 6-D 提示：审批批准后任务是 PENDING —— 下面这条 UPDATE 的条件匹配得上吗？
        cursor.execute("UPDATE tasks SET status = 'RUNNING' WHERE id = ? AND status IN ('FAILED', 'RUNNING','PENDING')", (task_id,))
        conn.commit()
        conn.close()

        # 5. 继续循环
        try:
            for iteration in range(start_iteration, self.max_iterations):
                if self._is_stop_requested():
                    return {
                        "success": False,
                        "task_id": self.task_id,
                        "answer": "任务已取消",
                        "trajectory": self.trajectory,
                        "iterations": iteration,
                        "cancelled": True,
                    }
                # 闸门②（7-A-2）：和闸门①同一份判断，同样紧贴 create() 之前。
                # 少装这一处 → 审批批准后 resume 的那条路可以绕过整个预算。
                stopped = self._budget_guard(iteration)
                if stopped:
                    return stopped

                call_started_at = utc_now_ms()             # 阶段 7-C：续跑同样要记延迟
                response = self.llm_client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": system_prompt}] + messages,
                    max_tokens=1024,
                    **self._tools_kwargs(),
                )
                message = response.choices[0].message
                usage = self._extract_usage(response)      # 阶段 7-A：续跑同样要记账

                # 和 run() 走同一份逻辑：工具调用 / 直接回答 / 空响应 三合一
                # 必须传 self.task_id：run() 的入参 task_id 可能是 None
                # （调用方没传时，任务行是 _create_task 建的，id 记在 self.task_id）
                result = self._handle_llm_message(
                    message, messages, system_prompt, iteration, self.task_id, usage,
                    call_started_at,
                )
                if result is not None:
                    # resume() 的结果里额外带上"从第几轮续跑"，方便调用方观察
                    result["resumed_from"] = start_iteration
                    return result

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
