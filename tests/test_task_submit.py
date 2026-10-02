import sqlite3

from app.task_submit import create_and_enqueue_task


class FakeQueue:
    def __init__(self):
        self.enqueued = []

    def lpush(self, name, task_id):
        self.enqueued.append((name, task_id))


def test_create_and_enqueue_task_writes_pending_then_enqueues(tmp_path):
    db_path = str(tmp_path / "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            question TEXT NOT NULL,
            status TEXT NOT NULL,
            max_iterations INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()

    queue = FakeQueue()
    result = create_and_enqueue_task(
        "测试异步任务",
        max_iterations=3,
        db_path=db_path,
        queue_client=queue,
    )

    assert result["status"] == "PENDING"
    assert len(queue.enqueued) == 1
    assert queue.enqueued[0][1] == result["task_id"]

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT question, status, max_iterations FROM tasks WHERE id = ?",
        (result["task_id"],),
    ).fetchone()
    conn.close()
    assert row == ("测试异步任务", "PENDING", 3)


def test_invalid_submission_is_rejected(tmp_path):
    db_path = str(tmp_path / "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, question TEXT, status TEXT, max_iterations INTEGER)")
    conn.commit()
    conn.close()

    queue = FakeQueue()
    for question, max_iterations in [("", 5), ("   ", 5), ("有效问题", 0), ("有效问题", -1)]:
        try:
            create_and_enqueue_task(question, max_iterations, db_path, queue)
        except ValueError:
            pass
        else:
            raise AssertionError("非法任务提交应该抛出 ValueError")
    assert queue.enqueued == []
