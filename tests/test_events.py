import json

from app.events import event_channel, publish_event


class FakeRedis:
    def __init__(self):
        self.published = []

    def publish(self, channel, payload):
        self.published.append((channel, payload))
        return 1


def test_publish_event_sends_json_to_task_channel():
    client = FakeRedis()
    publish_event("task-123", {"type": "step_completed", "iteration": 1}, client=client)
    assert len(client.published) == 1
    channel, payload = client.published[0]
    assert channel == event_channel("task-123")
    event = json.loads(payload)
    assert event["type"] == "step_completed"
    assert event["iteration"] == 1
    assert "ts" in event


def test_publish_event_does_not_overwrite_existing_timestamp():
    client = FakeRedis()
    publish_event("task-123", {"type": "task_completed", "ts": "fixed"}, client=client)
    event = json.loads(client.published[0][1])
    assert event["ts"] == "fixed"
