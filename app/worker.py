import logging
import signal
import os
from typing import Any, Callable

from dotenv import load_dotenv
from openai import OpenAI

from app.config import get_settings
from app.logging_setup import log_context
from app.queue import dequeue_task


# 用 __spec__.name 而不是 __name__：这两个进程都是用 `python -m app.xxx` 启动的，
# 那种情况下 __name__ 是 "__main__"，日志里的 logger 字段就变成 "__main__" ——
# worker 和 orchestrator 会**长得一模一样**，按 logger 过滤/分派日志全部失效。
# __spec__.name 拿到的永远是真实模块名（PEP 451）；普通 import 时两者相同。
logger = logging.getLogger(__spec__.name if __spec__ else __name__)


class Worker:
    """最小后台 worker：从队列取 task_id，再交给执行函数。"""

    def __init__(
        self,
        load_task: Callable[[str], dict | None],
        run_task: Callable[[dict], Any],
        queue_client: Any = None,
        on_error: Callable[[str, Exception], None] | None = None,
    ):
        self.load_task = load_task
        self.run_task = run_task
        self.queue_client = queue_client
        self.on_error = on_error
        self.running = True

    def stop(self, signum=None, frame=None):
        """收到 SIGTERM/SIGINT 后停止接收新任务。"""
        self.running = False

    def run_once(self, timeout: int = 0) -> bool:
        """处理一次队列消息；返回是否实际执行了任务。

        关键：单个任务失败**不能**让 worker 进程退出。
        2026-09-17 的事故就是：一个任务的 UNIQUE 冲突 → 异常处理里又撞上
        "database is locked" → 整个 worker 进程死掉，剩下所有任务都没人干。
        """
        task_id = dequeue_task(timeout=timeout, client=self.queue_client)
        if task_id is None:
            return False
        task = self.load_task(task_id)
        if task is None or task.get("status") != "PENDING":
            # 静默丢弃是排查噩梦：任务提交返回 202、却永远不执行，
            # 而所有日志里一个字都没有（2026-10-01 就这么被坑了半小时）。
            # task 为 None 意味着"队列里有、库里没有" —— 几乎一定是
            # 两个进程读的不是同一个库（DATABASE_PATH 配错/漏传）。
            logger.warning("task_skipped", extra={
                "task_id": task_id,
                "reason": "not_found_in_db" if task is None
                          else f"status={task.get('status')}",
            })
            return False
        # 最外层绑一次上下文：下面十几层函数的日志自动带上 task_id。这就是 contextvars
        # 存在的意义 —— 一处绑定、处处可用，不用把 task_id 穿过十几个函数签名。
        # 必须**离开**这个 with：worker 是长期进程，不清就会带着上一个任务的 task_id
        # 去记下一个任务的日志（串号的日志比没有日志更糟：你会照着错误的时间线排查）。
        with log_context(task_id=task_id):
            try:
                self.run_task(task)
            except Exception as error:
                # exc_info=True 才会带上堆栈，而且 JsonFormatter 会把它转义进同一行。
                # 原来那句 traceback.print_exc() 反而会往 stdout 打多行文本，
                # 破坏"一行一条事件"的前提 —— 既然堆栈已经进 JSON 了，就删掉它。
                logger.error("task_failed", extra={"error": str(error)}, exc_info=True)
                if self.on_error:
                    try:
                        self.on_error(task_id, error)
                    except Exception as rescue_error:
                        logger.error("rescue_mark_failed", extra={"error": str(rescue_error)}, exc_info=True)
                return False
        return True
        

    def run_forever(self, timeout: int = 5):
        """持续消费队列，直到收到停止信号。"""
        while self.running:
            self.run_once(timeout=timeout)


def install_signal_handlers(worker: Worker) -> None:
    """安装优雅停止信号处理器。"""
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)


def main() -> None:
    """组装真实依赖并启动后台 Worker。"""
    from app.queue import get_redis_client
    from app.worker_runtime import PersistentTaskRuntime
    from app.config import describe_settings
    from app.logging_setup import setup_logging

    load_dotenv()
    # 进程入口配置一次日志。LOG_PATH 默认 agent.log，本地 grep 用；
    # 容器里更常见的做法是不落盘、只写 stdout，由外部（Filebeat/Loki）收集。
    # 默认只输出到 stdout：容器里"进程只写 stdout、由外部收集"是标准做法，
    # 应用自己往容器文件系统写日志，等于把日志埋在一个会随容器一起消失的地方。
    # 本地想 grep 文件时显式设 LOG_PATH=agent.log。
    settings = get_settings()
    # 先配置日志，再打第一条日志（理由见 orchestrator.main 的同款注释）
    setup_logging(log_path=settings.log_path)
    logger.info("worker_starting", extra=describe_settings(settings))
    redis_client = get_redis_client()
    llm_client = OpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
    )
    runtime = PersistentTaskRuntime(
        db_path=settings.database_path,
        redis_client=redis_client,
        llm_client=llm_client,
    )
    worker = Worker(
        load_task=runtime.load_task,
        run_task=runtime.run_task,
        queue_client=redis_client,
        on_error=runtime.mark_failed,
    )
    install_signal_handlers(worker)
    worker.run_forever()


if __name__ == "__main__":
    main()
