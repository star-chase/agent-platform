import sqlite3
import tempfile
from pathlib import Path

from app.agent_fc_persistent import AgentFCPersistentExecutor


def test_stop_check_defaults_to_false():
    executor = AgentFCPersistentExecutor(llm_client=None)
    assert executor._is_stop_requested() is False


def test_stop_check_uses_injected_callback():
    executor = AgentFCPersistentExecutor(llm_client=None, should_stop=lambda: True)
    assert executor._is_stop_requested() is True


def test_cancelled_resume_must_not_restore_running_status():
    with tempfile.TemporaryDirectory() as directory:
        db_path = str(Path(directory) / "cancelled.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                status TEXT NOT NULL,
                answer TEXT,
                max_iterations INTEGER,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        conn.execute(
            "INSERT INTO tasks (id, question, status, max_iterations) VALUES (?, ?, ?, ?)",
            ("task-cancelled", "测试取消", "CANCELLED", 3),
        )
        conn.commit()
        conn.close()

        class FakeCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                raise AssertionError("CANCELLED 任务 resume 不应调用 LLM")

        completions = FakeCompletions()
        fake_client = type(
            "FakeClient",
            (),
            {"chat": type("FakeChat", (), {"completions": completions})()},
        )()

        executor = AgentFCPersistentExecutor(fake_client, db_path=db_path)
        result = executor.resume("task-cancelled")

        assert result["cancelled"] is True
        assert result["task_id"] == "task-cancelled"
        assert result["iterations"] == 0
        assert completions.calls == 0

        conn = sqlite3.connect(db_path)
        status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", ("task-cancelled",)
        ).fetchone()[0]
        conn.close()
        assert status == "CANCELLED"
