"""阶段 7-B 的副产品：一次真实联调暴露出来的**静默失败**。

2026-10-01 跑 7-B 联调时，日志里只出现了一条 `task_queued`，然后什么都没有 ——
任务提交返回 202，却**永远不执行、也没有任何报错**。

根因：`app/main.py` 的单 Agent 端点全都写死了 `sqlite3.connect("agent.db")`，
而 `POST /tasks` 调 `create_and_enqueue_task` 时又没传 `db_path`
（那个函数的默认值同样是 `"agent.db"`）。于是：

    API   → 把任务写进 agent.db
    worker→ 去 DATABASE_PATH（= agent_final.db）里找 → 找不到 → 静默 return False

只要 `DATABASE_PATH` 不是默认值（**每个真实部署都是**），单 Agent 那套端点就是坏的。

单测全绿、真实联调才炸 —— 和 7-A 的 `budget_usd` KeyError 同一族谱：
**同一份配置散落在多处，改一处漏一处。**

顺带修掉的第二个 bug：那 4 处 `sqlite3.connect()` 也绕过了 `app.db.connect()`，
所以既没有 `busy_timeout` 也没有 WAL —— 4 个进程并发时它们照样会
`database is locked`（阶段 3 花了整节课解决的那个问题，在 API 层悄悄复发）。
"""

import asyncio
import pathlib

from app.worker import Worker


def test_api_layer_never_hardcodes_a_database_path():
    """`app/main.py` 里不许出现库路径字面量，也不许绕过连接工厂。

    这不是"代码风格测试"，是一条**回归护栏**：上面那个 bug 之所以能活到
    真实联调，就是因为它只是几行 `sqlite3.connect("agent.db")`，
    看起来人畜无害，而单测根本走不到（它们都注入临时库路径）。
    """
    source = pathlib.Path("app/main.py").read_text(encoding="utf-8")

    assert '"agent.db"' not in source, "库路径只能来自 DATABASE_PATH，不能写死"
    assert "sqlite3" not in source, "一律走 app.db.connect（带 busy_timeout + WAL）"


def test_submit_task_passes_the_configured_db_path(tmp_path, monkeypatch):
    """`POST /tasks` 必须把当前配置的库路径传给服务层。

    不传的后果是任务写进默认库、worker 找不到 —— 而且**没有任何报错**。
    """
    import app.main as main

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "configured.db"))
    captured = {}

    def fake_submit(question, max_iterations, db_path=None, queue_client=None):
        captured["db_path"] = db_path
        return {"task_id": "t-1", "status": "PENDING"}

    monkeypatch.setattr(main, "create_and_enqueue_task", fake_submit)
    monkeypatch.setattr(main, "get_redis_client", lambda: object())

    asyncio.run(main.submit_task(
        main.TaskSubmitRequest(question="问题", max_iterations=3)
    ))

    assert captured["db_path"] == str(tmp_path / "configured.db")


def test_worker_logs_a_warning_when_it_cannot_run_a_dequeued_task(monkeypatch, tmp_path):
    """worker 丢弃一条队列消息时必须留痕。

    原来这里是一句 `return False`。于是"提交了但没执行"这种最难查的问题，
    在日志里**一个字都没有** —— 你只能靠猜。现在至少能 grep 到 task_skipped
    和原因（not_found_in_db = 队列里有、库里没有 = 两个进程读的不是同一个库）。
    """
    import io
    import json

    import app.worker as worker_module
    from app.logging_setup import setup_logging

    buffer = io.StringIO()
    setup_logging(level="INFO", stream=buffer)

    monkeypatch.setattr(worker_module, "dequeue_task",
                        lambda timeout=0, client=None: "task-from-queue")
    worker = Worker(load_task=lambda task_id: None, run_task=lambda task: None)

    assert worker.run_once() is False

    events = [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]
    skipped = next(event for event in events if event["event"] == "task_skipped")
    assert skipped["level"] == "WARNING"
    assert skipped["task_id"] == "task-from-queue"
    assert skipped["reason"] == "not_found_in_db"
