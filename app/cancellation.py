from datetime import datetime, timezone
from typing import Any


# BUDGET_EXCEEDED 也是终态（7-A-2：预算花光的任务不会自己再活过来）。
# 这份集合被 main.py 的 is_terminal 用来决定事件流什么时候收尾 ——
# 漏了它，超预算的任务 SSE 连接会一直挂着等一个永远不会来的下一步。
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED", "BUDGET_EXCEEDED"}


def is_task_cancelled(task: dict[str, Any]) -> bool:
    """判断任务是否已被取消。"""
    return task.get("status") == "CANCELLED"


def is_task_timed_out(
    task: dict[str, Any],
    now: datetime | None = None,
    timeout_seconds: float = 300,
) -> bool:
    """判断 RUNNING 任务是否超过允许执行时间。"""
    if task.get("status") != "RUNNING" or not task.get("started_at"):
        return False
    started_at = datetime.fromisoformat(task["started_at"].replace("Z", "+00:00"))
    current = now or datetime.now(timezone.utc)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return (current - started_at).total_seconds() > timeout_seconds


class TaskNotFoundError(Exception):
    """取消的任务不存在。"""


class TaskNotCancellableError(Exception):
    """任务已经处于终态，不能取消。"""


def cancel_task(task_id: str, db_path: str = "agent.db") -> dict[str, str]:
    """原子地把 PENDING/RUNNING 任务标记为 CANCELLED。"""
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if exists is None:
            raise TaskNotFoundError(task_id)
        cursor = conn.execute(
            "UPDATE tasks SET status = 'CANCELLED', updated_at = datetime('now') "
            "WHERE id = ? AND status IN ('PENDING', 'RUNNING')",
            (task_id,),
        )
        if cursor.rowcount != 1:
            raise TaskNotCancellableError(task_id)
        conn.commit()
        return {"task_id": task_id, "status": "CANCELLED"}
    finally:
        conn.close()
