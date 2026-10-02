from app.config import get_settings
from typing import Any


QUEUE_NAME = "agent:tasks"


def get_redis_client(url: str | None = None) -> Any:
    """创建 Redis 客户端；实际连接在第一次命令执行时发生。"""
    # 基础设施代码可以直接提供，但保留导入延迟，方便没有安装 redis 时运行离线测试。
    import redis

    return redis.Redis.from_url(
        url or get_settings().redis_url,
        decode_responses=True,
        # socket_timeout 要大于 BRPOP 的等待时间，避免两者同时到期。
        socket_connect_timeout=5,
        socket_timeout=30,
    )


def enqueue_task(task_id: str, client: Any = None) -> None:
    """把任务 ID 放入 Redis 队列。"""
    client = client or get_redis_client()
    client.lpush(QUEUE_NAME, task_id)


def dequeue_task(timeout: int = 5, client: Any = None) -> str | None:
    """从 Redis 队列阻塞式取出一个任务 ID。"""
    import redis

    client = client or get_redis_client()
    try:
        result = client.brpop(QUEUE_NAME, timeout=timeout)
    except redis.exceptions.TimeoutError:
        # 空闲轮询时，Redis/网络 socket 超时不应让 worker 进程退出。
        return None
    if result is None:
        return None
    _, task_id = result
    return task_id.decode() if isinstance(task_id, bytes) else task_id
