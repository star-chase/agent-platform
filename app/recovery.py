"""僵尸任务回收：RUNNING 卡死的标记失败，PENDING 丢消息的重新入队。

2026-09-17 的教训：worker 进程崩掉后，它正在执行的任务永远停在 `RUNNING`；
编排进程看到"这个节点已经派发过"就再也不会重派 → workflow 永久卡在 RUNNING。
所以必须有一个兜底的"收割者"。

注意取舍：判断依据是 `updated_at`（任务启动时间），所以阈值要明显大于
一个正常任务的最长耗时，否则会把还在正常干活的慢任务误杀。
"""

import os
import logging
from collections.abc import Callable

from app.config import get_settings
from app.db import connect

DEFAULT_STALE_MINUTES = 15

logger = logging.getLogger(__name__)


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

def recover_orphan_pending_tasks(
    db_path: str | None = None,
    older_than_seconds: int | None = None,
    enqueue: Callable[[str], None] | None = None,
) -> list[str]:
    """把"消息丢了、但数据库里还是 PENDING"的孤儿任务重新入队，返回被重投的 task_id。

    为什么需要它（2026-10-10 故障注入实测）：
    Redis 里的队列消息**不是持久事实**。Redis 被 kill（非正常宕机）后，LPUSH 进去的
    task_id 全没了；而 tasks 表里这些任务仍是 PENDING、node 也已经在 dispatched 集合里，
    调度器不会再派发它们 —— 整条流水线永久停在 RUNNING。
    `recover_stale_tasks` 只处理卡在 RUNNING 的任务，覆盖不到这条路径。

    为什么安全：worker 取到消息后**先查状态**，不是 PENDING 就跳过（app/worker.py 的
    task_skipped）。所以重复入队最多是白跑一次 BRPOP，不会重复执行同一个任务。

    Args:
        older_than_seconds: 只重投"比这个时间更老"的 PENDING 任务，避免和刚入队、
            正在被 worker 抢的任务撞车。默认取配置 `orphan_task_recovery_seconds`；
            <= 0 表示关闭（返回空列表）。
        enqueue: 入队函数（默认走 Redis 队列）；注入它是为了离线测试。
    """
    seconds = (
        older_than_seconds
        if older_than_seconds is not None
        else get_settings().orphan_task_recovery_seconds
    )
    if not seconds or seconds <= 0:
        return []

    if enqueue is None:
        from app.queue import enqueue_task  # 延迟导入：没有装 redis 时也能离线用

        enqueue = lambda task_id: enqueue_task(task_id)  # noqa: E731

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status = 'PENDING' AND updated_at < datetime('now', ?)",
            (f"-{seconds} seconds",),
        ).fetchall()
        task_ids = [row[0] for row in rows]
    finally:
        conn.close()

    for task_id in task_ids:
        enqueue(task_id)
    if task_ids:
        # 把 updated_at 当作"这次补偿的时间戳"：不这么做的话，orchestrator 每个 tick
        # （默认 2 秒）都会把同一批 PENDING 任务再投一遍，队列被刷爆、worker 反复空弹。
        # 补偿之后时间戳往前推，下一次补偿至少要再等 older_than_seconds。
        conn = connect(db_path)
        try:
            conn.executemany(
                "UPDATE tasks SET updated_at = datetime('now') WHERE id = ?",
                [(task_id,) for task_id in task_ids],
            )
            conn.commit()
        finally:
            conn.close()
        logger.warning("orphan_pending_requeued", extra={
            "count": len(task_ids),
            "older_than_seconds": seconds,
            "task_ids": task_ids[:10],
        })
    return task_ids
