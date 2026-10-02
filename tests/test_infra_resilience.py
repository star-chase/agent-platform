"""基础设施回归测试：worker 抗错 + 僵尸任务回收。

对应 2026-09-17 真实联调暴露的两个问题：
1. 一个任务执行出错会打死整个 worker 进程；
2. worker 崩掉后任务永远卡在 RUNNING，编排进程再也不会重派 → workflow 永久卡住。
"""

import sqlite3

from app.db import connect, ensure_schema
from app.queue import QUEUE_NAME
from app.recovery import recover_stale_tasks
from app.worker import Worker


class FakeRedis:
    def __init__(self, values=None):
        self.values = list(values or [])

    def brpop(self, name, timeout):
        assert name == QUEUE_NAME
        if not self.values:
            return None
        return name, self.values.pop(0)


def test_worker_survives_task_failure():
    """任务抛异常时 worker 不能退出，还要调用兜底回调。"""
    errors = []

    def boom(_task):
        raise RuntimeError("任务炸了")

    worker = Worker(
        lambda _id: {"id": "task-1", "status": "PENDING"},
        boom,
        queue_client=FakeRedis(["task-1"]),
        on_error=lambda task_id, error: errors.append((task_id, str(error))),
    )

    assert worker.run_once() is False          # 没执行成功
    assert errors == [("task-1", "任务炸了")]   # 兜底回调被调用
    assert worker.running is True              # 进程状态仍然存活 → run_forever 会继续下一轮


def test_worker_marks_failed_even_if_on_error_missing():
    """没传 on_error 时也不能因为异常而崩，只是没兜底标记。"""
    worker = Worker(
        lambda _id: {"id": "task-1", "status": "PENDING"},
        lambda _task: (_ for _ in ()).throw(RuntimeError("炸")),
        queue_client=FakeRedis(["task-1"]),
    )
    assert worker.run_once() is False
    assert worker.running is True


def make_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = connect(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    return db_path


def insert_running_task(db_path, task_id, minutes_ago):
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, question, status, updated_at) "
            "VALUES (?, '任务', 'RUNNING', datetime('now', ?))",
            (task_id, f"-{minutes_ago} minutes"),
        )
        conn.commit()
    finally:
        conn.close()


def read_status(db_path, task_id):
    conn = connect(db_path)
    try:
        return conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
    finally:
        conn.close()


def test_recover_stale_tasks_fails_only_old_running_tasks(tmp_path):
    db_path = make_db(tmp_path)
    insert_running_task(db_path, "zombie", 60)      # 卡了 60 分钟 → 该回收
    insert_running_task(db_path, "fresh", 1)        # 才 1 分钟 → 不能误杀

    recovered = recover_stale_tasks(db_path, timeout_minutes=15)

    assert recovered == ["zombie"]
    assert read_status(db_path, "zombie") == "FAILED"
    assert read_status(db_path, "fresh") == "RUNNING"


def test_recover_stale_tasks_ignores_completed_tasks(tmp_path):
    db_path = make_db(tmp_path)
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, question, status, updated_at) "
            "VALUES ('done', '任务', 'COMPLETED', datetime('now', '-120 minutes'))"
        )
        conn.commit()
    finally:
        conn.close()

    assert recover_stale_tasks(db_path, timeout_minutes=15) == []
    assert read_status(db_path, "done") == "COMPLETED"
