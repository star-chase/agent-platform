import sqlite3

from app.cancellation import TaskNotCancellableError, TaskNotFoundError, cancel_task


def create_database(path):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def test_pending_task_can_be_cancelled(tmp_path):
    db_path = str(tmp_path / "test.db")
    create_database(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO tasks (id, status) VALUES ('task-1', 'PENDING')")
    conn.commit()
    conn.close()

    result = cancel_task("task-1", db_path=db_path)
    assert result == {"task_id": "task-1", "status": "CANCELLED"}

    conn = sqlite3.connect(db_path)
    status = conn.execute("SELECT status FROM tasks WHERE id = 'task-1'").fetchone()[0]
    conn.close()
    assert status == "CANCELLED"


def test_completed_task_cannot_be_cancelled(tmp_path):
    db_path = str(tmp_path / "test.db")
    create_database(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO tasks (id, status) VALUES ('task-1', 'COMPLETED')")
    conn.commit()
    conn.close()

    try:
        cancel_task("task-1", db_path=db_path)
    except TaskNotCancellableError:
        pass
    else:
        raise AssertionError("COMPLETED 任务不应该允许取消")


def test_missing_task_cannot_be_cancelled(tmp_path):
    db_path = str(tmp_path / "test.db")
    create_database(db_path)

    try:
        cancel_task("missing", db_path=db_path)
    except TaskNotFoundError:
        pass
    else:
        raise AssertionError("不存在任务应该抛 TaskNotFoundError")
