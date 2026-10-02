import json
import logging
import sqlite3
from typing import Any
import os

from app.config import get_settings
from app.agent_fc_persistent import AgentFCPersistentExecutor
from app.approvals import create_approval_request
from app.agent_profiles import AgentProfile, get_profile
from app.db import connect as connect_db, utc_now_ms
from app.events import publish_event, publish_workflow_event
from app.logging_setup import log_context


logger = logging.getLogger(__name__)


class PersistentTaskRuntime:
    """把通用 Worker 和 SQLite 持久化 Agent 接起来。

    阶段 5-E-2：这里还要负责"给子任务装配角色"——
    子任务在 DAG 里的节点带着 agent_type，runtime 按它把对应的
    system prompt 和工具集装进执行器，这样同一个 workflow 里
    researcher / analyst / writer 才会真的用不同的工具。
    """

    def __init__(self, db_path: str = "agent.db", redis_client: Any = None, llm_client: Any = None):
        self.db_path = db_path
        self.redis_client = redis_client
        self.llm_client = llm_client

    def load_task(self, task_id: str) -> dict[str, Any] | None:
        """从数据库读取队列任务。"""
        conn = connect_db(self.db_path, row_factory=True)
        try:
            row = conn.execute(
                "SELECT id, workflow_id, node_id, question, status, max_iterations "
                "FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def mark_failed(self, task_id: str, error: Exception) -> None:
        """任务异常退出时的兜底：把还在 PENDING/RUNNING 的任务标记为 FAILED。

        正常情况下执行器自己会写 FAILED；但如果是执行器之外的错误
        （例如数据库锁、序列化失败），这里保证任务不会永远卡在 RUNNING。
        """
        conn = connect_db(self.db_path)
        try:
            conn.execute(
                "UPDATE tasks SET status = 'FAILED', answer = ?, updated_at = datetime('now') "
                "WHERE id = ? AND status IN ('PENDING', 'RUNNING')",
                (f"执行出错: {error}", task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _resolve_profile(self, task: dict[str, Any]) -> AgentProfile | None:
        """按子任务节点的 agent_type 找出角色档案；查不到返回 None（表示用默认行为）。

        取不到（老任务缺字段 / workflow 不存在 / DAG 里没这个节点）时返回 None，
        调用方就退回"默认提示词 + 全部工具"。
        """
        workflow_id = task.get("workflow_id")
        node_id = task.get("node_id")
        if not workflow_id or not node_id:
            return None
        conn = connect_db(self.db_path, row_factory=True)
        try:
            row = conn.execute(
                "SELECT dag FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        dag = json.loads(row["dag"])
        for node in dag.get("nodes", []):
            if node.get("id") == node_id:
                return get_profile(node.get("agent_type"))
        return None
      

    def get_status(self, task_id: str) -> str | None:
        task = self.load_task(task_id)
        return task["status"] if task else None

    def should_stop(self, task_id: str) -> bool:
        """取消或任务消失时，在 Agent 安全点停止。"""
        return self.get_status(task_id) in {None, "CANCELLED"}

    def _has_steps(self, task_id: str) -> bool:
        """这个任务是否已经有执行记录（= 它是续跑，不是第一次跑）。

        阶段 6-D 用它区分 run / resume 两条路：
        审批回来重新入队的任务，steps 表里已经有历史了。
        """
        conn = connect_db(self.db_path, row_factory=True)
        try:
            row = conn.execute(
                "SELECT 1 FROM steps WHERE task_id = ? LIMIT 1", (task_id,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def _mark_task_started(self, task_id: str) -> None:
        """记下这次尝试真正开始的时刻（阶段 7-C）。

        COALESCE 而不是直接赋值：审批暂停后 resume 会再进一次 run_task，
        但"这个任务是什么时候开始干的"应该保持第一次的值 ——
        否则 trace 上会看到同一次执行被切成两段。
        """
        conn = connect_db(self.db_path)
        try:
            conn.execute(
                "UPDATE tasks SET started_at = COALESCE(started_at, ?) WHERE id = ?",
                (utc_now_ms(), task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _mark_task_finished(self, task_id: str) -> None:
        """记下这次尝试结束的时刻。放在 finally 里：崩了也要留下结束时间。

        不记的后果是 trace 上这一段的耗时显示为"未知"，而"未知"恰恰出现在
        你最需要它的那次（失败的那次）。
        """
        conn = connect_db(self.db_path)
        try:
            conn.execute(
                "UPDATE tasks SET finished_at = ? WHERE id = ?", (utc_now_ms(), task_id)
            )
            conn.commit()
        finally:
            conn.close()

    def run_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """创建持久化执行器并运行一个任务。

        整个方法体绑在 log_context 里：入口绑一次，下面十几层（执行器、工具调用、
        审批、成本闸门）打出来的日志自动带上 task_id / workflow_id / node_id。
        """
        task_id = task["id"]
        # 用 with 而不是 bind_context + clear_context 两行：with 在异常和提前 return
        # 时都会恢复。手写 clear 只要漏在某个 return 之前，上下文就泄漏给下一个任务，
        # 而串号的日志比没有日志更难查（你会照着一份错误的时间线去排查）。
        with log_context(
            task_id=task_id,
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node_id"),
        ):
            logger.info("task_started")
            task_id = task["id"]

            def on_step(event):
                publish_event(task_id, event, client=self.redis_client)

            # 按节点角色装配：writer 拿不到工具，analyst 拿到 calculator+search
            profile = self._resolve_profile(task)

            executor = AgentFCPersistentExecutor(
                llm_client=self.llm_client,
                model=get_settings().deepseek_model,
                max_iterations=task["max_iterations"],
                db_path=self.db_path,
                on_step=on_step,
                should_stop=lambda: self.should_stop(task_id),
                system_prompt=profile.system_prompt if profile else None,
                tool_names=profile.tools if profile else None,
                # 阶段 6-D：Agent 自己请求人工审批时，用它建审批记录
                request_approval=lambda requested_task_id, reason: create_approval_request(
                    self.db_path, requested_task_id, task.get("workflow_id"),
                    task.get("node_id"), reason,
                ),
            )

            # 已经有执行记录 = 续跑（审批批准后重新入队 / 失败重试），必须 resume：
            # run() 会从 iteration 1 重新写 step，直接撞 UNIQUE(task_id, iteration)
            self._mark_task_started(task_id)
            try:
                if self._has_steps(task_id):
                    result = executor.resume(task_id)
                else:
                    result = executor.run(task["question"], task_id=task_id)
            finally:
                # finally 而不是"跑完再记"：异常路径同样要留下结束时间，
                # 否则 trace 上恰好是"失败的那次"没有耗时 —— 最需要它的时候没有。
                self._mark_task_finished(task_id)

            # 只有属于某个 workflow 的节点任务才发（阶段 4 的老任务不在 DAG 里）；
            # status 从库里读真实值：CANCELLED / FAILED 的语义只有数据库知道
            # status 从库里读真实值：CANCELLED / FAILED / WAITING_APPROVAL 只有数据库知道。
            # 提前算一次，日志和事件共用同一个值（别查两遍，两遍之间状态可能已经变了）
            status = self.get_status(task_id)
            logger.info("task_completed", extra={"status": status})

            if task.get("workflow_id"):
                workflow_id = task["workflow_id"]
                event = {
                    "type": "node_finished",
                    "node_id": task.get("node_id"),
                    "task_id": task_id,
                    "status": status,
                }
                publish_workflow_event(workflow_id, event, client=self.redis_client)
            return result
