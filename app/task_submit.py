import logging
import sqlite3
import uuid
from typing import Any

from app.queue import enqueue_task


logger = logging.getLogger(__name__)


def create_and_enqueue_task(
    question: str,
    max_iterations: int = 5,
    db_path: str = "agent.db",
    queue_client: Any = None,
) -> dict[str, Any]:
    """创建 PENDING 任务并入队，返回 task_id。"""
    if not question.strip():
        raise ValueError("question 不能为空")
    if not isinstance(max_iterations, int) or max_iterations <= 0:
        raise ValueError("max_iterations 必须是正整数")
    task_id = str(uuid.uuid4())
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, question, status, max_iterations) VALUES (?, ?, 'PENDING', ?)",
            (task_id, question, max_iterations),
        )
        conn.commit()
    finally:
        conn.close()
    enqueue_task(task_id, client=queue_client)
    # "提交了但一直没跑"是最常见的排查起点：先确认它在日志里出现过。
    logger.info("task_queued", extra={"task_id": task_id, "max_iterations": max_iterations})
    return {"task_id": task_id, "status": "PENDING"}
