from app.queue import QUEUE_NAME, dequeue_task, enqueue_task


class FakeRedis:
    """只模拟本节课需要的 LPUSH / BRPOP，避免测试依赖真实 Redis。"""

    def __init__(self):
        self.values = []

    def lpush(self, name, value):
        assert name == QUEUE_NAME
        self.values.insert(0, value)

    def brpop(self, name, timeout):
        assert name == QUEUE_NAME
        if not self.values:
            return None
        return name, self.values.pop()


def test_enqueue_and_dequeue_round_trip():
    client = FakeRedis()
    enqueue_task("task-123", client=client)
    assert dequeue_task(timeout=0, client=client) == "task-123"


def test_dequeue_empty_queue_returns_none():
    client = FakeRedis()
    assert dequeue_task(timeout=0, client=client) is None


def test_queue_is_fifo_for_multiple_tasks():
    client = FakeRedis()
    enqueue_task("task-1", client=client)
    enqueue_task("task-2", client=client)
    assert dequeue_task(timeout=0, client=client) == "task-1"
    assert dequeue_task(timeout=0, client=client) == "task-2"


def test_redis_timeout_is_treated_as_empty_poll():
    class TimeoutRedis:
        def brpop(self, name, timeout):
            import redis
            raise redis.exceptions.TimeoutError("socket timeout")

    assert dequeue_task(timeout=5, client=TimeoutRedis()) is None
