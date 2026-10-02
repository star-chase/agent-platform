import tempfile
from pathlib import Path

from app.agent_fc_persistent import AgentFCPersistentExecutor
from app.db import connect, ensure_schema


def test_run_with_existing_task_id_does_not_create_second_task():
    """用 fake LLM 验证复用 task_id 时不会新增第二条任务。"""
    with tempfile.TemporaryDirectory() as directory:
        db_path = str(Path(directory) / "reuse.db")
        # 统一走 ensure_schema，别再手写 CREATE TABLE（影子 schema 会随项目演进过期）
        conn = connect(db_path)
        try:
            ensure_schema(conn)
            conn.execute(
                "INSERT INTO tasks (id, question, status, max_iterations) VALUES (?, ?, 'PENDING', 1)",
                ("task-existing", "直接回答你好"),
            )
            conn.commit()
        finally:
            conn.close()

        class Message:
            tool_calls = None
            content = "你好"

            def model_dump(self):
                return {"content": self.content, "tool_calls": None}

        class Completions:
            def create(self, **kwargs):
                return type("Response", (), {"choices": [type("Choice", (), {"message": Message()})()]})()

        client = type("Client", (), {"chat": type("Chat", (), {"completions": Completions()})()})()
        executor = AgentFCPersistentExecutor(client, db_path=db_path, max_iterations=1)
        result = executor.run("直接回答你好", task_id="task-existing")

        assert result["task_id"] == "task-existing"
        conn = connect(db_path)
        try:
            rows = conn.execute("SELECT id, status FROM tasks").fetchall()
        finally:
            conn.close()
        # 阶段 8-0：行对象从"元组"变成 Row（既支持下标也支持列名，两个方言统一）。
        # 契约变了，断言就得跟着变 —— 用 tuple(row) 把"值"拿出来比，别去比对象的类型。
        assert [tuple(row) for row in rows] == [("task-existing", "COMPLETED")]
