"""僵尸任务回收：把卡在 RUNNING 太久的任务标记为 FAILED。

2026-09-17 的教训：worker 进程崩掉后，它正在执行的任务永远停在 `RUNNING`；
编排进程看到"这个节点已经派发过"就再也不会重派 → workflow 永久卡在 RUNNING。
所以必须有一个兜底的"收割者"。

注意取舍：判断依据是 `updated_at`（任务启动时间），所以阈值要明显大于
一个正常任务的最长耗时，否则会把还在正常干活的慢任务误杀。
"""

import os

from app.config import get_settings
from app.db import connect

DEFAULT_STALE_MINUTES = 15


def recover_stale_tasks(db_path: str | None = None, timeout_minutes: int | None = None) -> list[str]:
    """把卡在 RUNNING 超过 timeout_minutes 的任务标记为 FAILED，返回被回收的 task_id。"""
    minutes = timeout_minutes or get_settings().stale_task_minutes
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status = 'RUNNING' AND updated_at < datetime('now', ?)",
            (f"-{minutes} minutes",),
        ).fetchall()
        task_ids = [row[0] for row in rows]
        if task_ids:
            conn.executemany(
                "UPDATE tasks SET status = 'FAILED', answer = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                [(f"任务卡在 RUNNING 超过 {minutes} 分钟，已被回收", task_id) for task_id in task_ids],
            )
            conn.commit()
        return task_ids
    finally:
        conn.close()
