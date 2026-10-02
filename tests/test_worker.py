from app.worker import Worker
from app.queue import QUEUE_NAME


class FakeRedis:
    def __init__(self, values=None):
        self.values = list(values or [])

    def brpop(self, name, timeout):
        assert name == QUEUE_NAME
        if not self.values:
            return None
        return name, self.values.pop(0)


def test_worker_executes_pending_task_once():
    executed = []
    tasks = {"task-1": {"id": "task-1", "status": "PENDING"}}
    worker = Worker(tasks.get, executed.append, queue_client=FakeRedis(["task-1"]))

    assert worker.run_once() is True
    assert executed == [tasks["task-1"]]


def test_worker_skips_non_pending_task():
    executed = []
    tasks = {"task-1": {"id": "task-1", "status": "COMPLETED"}}
    worker = Worker(tasks.get, executed.append, queue_client=FakeRedis(["task-1"]))

    assert worker.run_once() is False
    assert executed == []


def test_worker_returns_false_when_queue_empty():
    executed = []
    worker = Worker(lambda _: None, executed.append, queue_client=FakeRedis())

    assert worker.run_once() is False
    assert executed == []
