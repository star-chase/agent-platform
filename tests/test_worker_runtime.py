"""worker runtime 的基础行为测试。

⚠️ 建表统一走 `app.db.ensure_schema`，**不要再手写 CREATE TABLE**。
这份文件原来自己写了一张最小 tasks 表，阶段 5-E-2 让 `load_task` 开始读
`workflow_id`/`node_id` 之后，它立刻炸了：`no such column: workflow_id`。
——和"run()/resume() 两份循环"是同一类病：**同一份定义存了两份，改一处漏一处**。
"""

from app.db import connect, ensure_schema
from app.worker_runtime import PersistentTaskRuntime


def make_db(tmp_path, task_id="task-1", status="PENDING"):
    db_path = str(tmp_path / "runtime.db")
    conn = connect(db_path)
    try:
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO tasks (id, question, status, max_iterations) VALUES (?, '问题', ?, 5)",
            (task_id, status),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_load_task_and_stop_check(tmp_path):
    db_path = make_db(tmp_path)

    runtime = PersistentTaskRuntime(db_path=db_path)
    task = runtime.load_task("task-1")

    assert task["question"] == "问题"
    assert runtime.should_stop("task-1") is False


def test_cancelled_task_should_stop(tmp_path):
    db_path = make_db(tmp_path, status="CANCELLED")

    runtime = PersistentTaskRuntime(db_path=db_path)

    assert runtime.should_stop("task-1") is True
