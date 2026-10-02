"""Workflow 与子任务的 SQLite 存储层。

阶段 5-D-2：调度器不直接写 SQL，所有"查库/建行"都经过这一层。
好处：5-D-3 的调度主循环可以注入一个假 store 做纯离线测试，
也把 SQL 细节和调度逻辑解耦（和阶段 4-C 给执行器注入 on_step 是同一个思路）。
"""

import json
import sqlite3
import uuid
from typing import Any

from app.db import connect as connect_db
from app.db import ensure_schema
from app.workflow_model import TASK_STATUSES, WORKFLOW_STATUSES


def ensure_workflow_schema(conn: sqlite3.Connection) -> None:
    """幂等地建表（保留旧名字，实际逻辑已合并进 `app.db.ensure_schema`）。"""
    ensure_schema(conn)


class SQLiteWorkflowStore:
    """workflows 表 + tasks 表（workflow 部分）的读写。"""

    def __init__(self, db_path: str = "agent.db"):
        self.db_path = db_path
        conn = connect_db(db_path)
        try:
            ensure_schema(conn)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return connect_db(self.db_path, row_factory=True)

    def create_workflow(
        self, goal: str, dag: dict[str, Any], status: str = "PLANNING",
        budget_usd: float | None = None,
    ) -> str:
        """创建一个 workflow，返回 workflow_id。

        budget_usd（阶段 7-A-2）：整个 workflow 的成本上限，None = 不限量。
        预算挂在 workflow 上而不是散在节点上：一条流水线是一个"预算单元"，
        按节点各设一份既没人算得清总额，也没法在派发下一个节点前判断还剩多少。
        """
        if not goal or not goal.strip():
            raise ValueError("goal 不能为空")
        if status not in WORKFLOW_STATUSES:
            raise ValueError(f"非法 workflow 状态：{status}")
        workflow_id = str(uuid.uuid4())
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO workflows (id, goal, status, dag, budget_usd) VALUES (?, ?, ?, ?, ?)",
                (workflow_id, goal, status, json.dumps(dag, ensure_ascii=False), budget_usd),
            )
            conn.commit()
        finally:
            conn.close()
        return workflow_id

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """读取 workflow，dag 字段已还原成 dict；不存在返回 None。"""
        conn = self._connect()
        try:
            row = conn.execute(
                # budget_usd 是阶段 7-A 加的列：**加列只是第一步，读路径也要跟上** ——
                # 漏了它端点会直接 KeyError 500（这个 bug 是真实联调抓到的，单测全绿）。
                "SELECT id, goal, status, dag, final_output, created_at, updated_at, budget_usd "
                "FROM workflows WHERE id = ?",
                (workflow_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        workflow = dict(row)
        workflow["dag"] = json.loads(workflow["dag"])
        return workflow

    def update_workflow_status(
        self, workflow_id: str, status: str, final_output: str | None = None
    ) -> None:
        """更新 workflow 状态（可选同时写 final_output）。"""
        if status not in WORKFLOW_STATUSES:
            raise ValueError(f"非法 workflow 状态：{status}")
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE workflows SET status = ?, final_output = COALESCE(?, final_output), "
                "updated_at = datetime('now') WHERE id = ?",
                (status, final_output, workflow_id),
            )
            conn.commit()
        finally:
            conn.close()

    def create_node_task(
        self,
        workflow_id: str,
        node_id: str,
        question: str,
        depends_on: list[str],
        upstream_outputs: dict[str, str] | None = None,
        max_iterations: int = 5,
        status: str = "PENDING",
    ) -> str:
        """为 DAG 的一个节点创建子任务，返回 task_id。

        depends_on / upstream_outputs 是 Python 对象，SQLite 只能存文本，
        所以用 json.dumps(..., ensure_ascii=False) 序列化（换 PG 时这两个字段变 JSONB，
        对外接口不变）。

        status 默认 PENDING；阶段 6-B 需要用 "WAITING_APPROVAL" 建行
        —— 节点需要人工审批时，它**一出生就在等人**，而不是先入队再暂停。
        """
        # 白名单式入口：不能让任意字符串写进状态列
        if status not in TASK_STATUSES:
            raise ValueError(f"非法 task 状态：{status}")
        task_id = str(uuid.uuid4())
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO tasks (id, question, status, max_iterations, workflow_id, node_id, "
                "depends_on, upstream_outputs) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id, question, status, max_iterations, workflow_id, node_id,
                    json.dumps(depends_on, ensure_ascii=False),
                    json.dumps(upstream_outputs or {}, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return task_id

    def list_node_tasks(self, workflow_id: str) -> list[dict[str, Any]]:
        """列出某个 workflow 的所有子任务（depends_on/upstream_outputs 还原成对象）。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                # rowid 做次级排序键：created_at 只精确到秒，同一秒内建的多条
                # 尝试顺序不确定——而"最新一次尝试"必须严格按插入顺序判定。
                # ⚠️ 这串列名是写死的 —— 7-C 加 started_at/finished_at 时必须回来补上，
                # 否则 trace 里每个节点都显示"耗时为 None"（7-A 的 budget_usd KeyError 同款）。
                "SELECT rowid, id, node_id, question, status, answer, depends_on, "
                "upstream_outputs, started_at, finished_at "
                "FROM tasks WHERE workflow_id = ? ORDER BY created_at ASC, rowid ASC",
                (workflow_id,),
            ).fetchall()
        finally:
            conn.close()
        tasks = []
        for row in rows:
            task = dict(row)
            task["depends_on"] = json.loads(task["depends_on"] or "[]")
            task["upstream_outputs"] = json.loads(task["upstream_outputs"] or "{}")
            tasks.append(task)
        return tasks

    def get_scheduling_state(self, workflow_id: str) -> dict[str, Any]:
        """汇总调度器需要的全部信息（5-D-3 主循环的唯一数据入口）。

        返回：
            completed:  最新一次尝试是 COMPLETED 的节点 id 集合
            failed:     最新一次尝试是 FAILED 的节点 id 集合
            fatal:      （7-A-2）最新一次尝试是 BUDGET_EXCEEDED 的节点 id 集合。
                        **确定性失败**：调度器见到就必须判死 workflow，不能重派
            dispatched: 已经建过 task 行的节点 id 集合（含所有状态）
            outputs:    {节点 id: answer}，只含最新一次尝试 COMPLETED 且有 answer 的
            attempts:   {节点 id: 已创建的 task 行数}，节点级重试（5-G）要用它

        为什么是"最新一次尝试说了算"（5-G 的关键）：
        节点重试会**新建**一条 task 行，同一个 node_id 于是可能同时存在
        FAILED（第 1 次）和 COMPLETED（第 2 次）两条记录。
        如果还按"扫描所有行、见到 FAILED 就记一笔"，这个节点既在 completed
        又在 failed，调度器会把已经跑成功的 workflow 判成失败。
        """
        conn = self._connect()
        try:
            # ORDER BY rowid：建行顺序就是尝试的先后顺序，所以最后读到的
            # 一行天然是这个节点的最新一次尝试。
            rows = conn.execute(
                "SELECT rowid, node_id, status, answer FROM tasks "
                "WHERE workflow_id = ? ORDER BY rowid ASC",
                (workflow_id,),
            ).fetchall()
        finally:
            conn.close()
        completed, failed, dispatched, outputs, attempts = set(), set(), set(), {}, {}
        fatal: set[str] = set()          # 7-A-2：BUDGET_EXCEEDED 的节点
        for row in rows:
            if row["node_id"] is None:
                continue
            node_id = row["node_id"]
            dispatched.add(node_id)
            attempts[node_id] = attempts.get(node_id, 0) + 1
            # 顺序遍历 + 覆盖式更新：最后留下的就是"最新一次尝试"的状态
            if row["status"] == "COMPLETED":
                completed.add(node_id)
                failed.discard(node_id)
                fatal.discard(node_id)      # 7-A-2：三个集合都要互斥，见下面注释
                if row["answer"]:
                    outputs[node_id] = row["answer"]
            elif row["status"] == "FAILED":
                failed.add(node_id)
                completed.discard(node_id)
                fatal.discard(node_id)
                outputs.pop(node_id, None)
            elif row["status"] == "BUDGET_EXCEEDED":
                # 7-A-2：确定性失败，单独成 fatal。
                # 它**不能**同时进 failed：进了 failed 就会被 tick() 当成"还有重试额度"
                # 重新派发，而超支的节点重试一次还是超支（白烧时间、白写日志）。
                fatal.add(node_id)
                completed.discard(node_id)
                failed.discard(node_id)
                outputs.pop(node_id, None)
            else:
                # "最新一次尝试说了算"这条不变式对**三个**集合都要成立：
                # 每个分支都得把另外两个清掉，否则同一个节点会同时出现在两个集合里 ——
                # 5-G 就是被这个坑过（既在 completed 又在 failed，调度器把跑成功的
                # workflow 判成失败）。漏掉 fatal.discard 会重现同一个 bug：
                # 先超支、后来（人类调高预算后）跑成功的节点，会把整条流水线判死。
                completed.discard(node_id)
                failed.discard(node_id)
                fatal.discard(node_id)
                outputs.pop(node_id, None)
        return {
            "completed": completed,
            "failed": failed,
            "fatal": fatal,
            "dispatched": dispatched,
            "outputs": outputs,
            "attempts": attempts,
        }

    def list_workflow_ids_by_status(self, status: str) -> list[str]:
        """列出某个状态的所有 workflow id（编排进程用它找出待推进的 workflow）。"""
        if status not in WORKFLOW_STATUSES:
            raise ValueError(f"非法 workflow 状态：{status}")
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id FROM workflows WHERE status = ? ORDER BY created_at ASC",
                (status,),
            ).fetchall()
        finally:
            conn.close()
        return [row["id"] for row in rows]

