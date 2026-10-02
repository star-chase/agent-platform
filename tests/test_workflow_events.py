"""阶段 5-H：workflow 级 SSE（全量快照 + 实时事件）。

这个文件的假 pubsub 刻意如实模拟 Redis Pub/Sub 的一条硬语义：
**订阅之前发布的消息，订阅者永远收不到**（Pub/Sub 不保存历史）。
"订阅空窗"这个 bug 只有在假对象如实建模这条语义时才会暴露出来——
假对象太好说话，测试就会骗你。
"""

import asyncio
import json

from app.sse import event_stream
from app.workflow_events import build_snapshot


class FakePubSub:
    """假 pubsub：只有"已订阅"状态下发布的消息才会进缓冲队列。"""

    def __init__(self):
        self.open = False
        self.buffered: list[str] = []
        self.closed = False

    def publish(self, payload: str) -> None:
        """模拟别的进程往这个频道发布一条事件。"""
        if self.open:                 # 订阅前发布的 → 永久丢失（真实语义）
            self.buffered.append(payload)

    def get_message(self, **kwargs):
        if self.buffered:
            return {"data": self.buffered.pop(0)}
        return None

    def subscribe(self, channel):
        self.open = True
        return self

    def close(self):
        self.open = False
        self.closed = True


async def read_from(source):
    return source.get_message(ignore_subscribe_messages=True, timeout=1.0)


# ---------------------------------------------------------------------------
# H1：event_stream 的订阅空窗 + 频道泛化
# ---------------------------------------------------------------------------

def test_event_stream_subscribes_before_loading_snapshot():
    """订阅必须先于读快照，否则读快照期间发布的事件永久丢失。"""
    calls: list[str] = []
    pubsub = FakePubSub()

    async def collect():
        return [item async for item in event_stream(
            "wf-1",
            load_history=lambda _: calls.append("history") or [{"type": "workflow_state"}],
            subscribe=lambda ch: calls.append("subscribe") or pubsub.subscribe(ch),
            is_terminal=lambda _: True,          # 终态：发完快照就该收尾
            read_message=read_from,
        )]

    events = asyncio.run(collect())

    assert calls == ["subscribe", "history"]
    assert len(events) == 1
    assert pubsub.closed is True                 # 终态提前 return 也不能漏掉 close


def test_event_stream_does_not_lose_events_published_during_snapshot():
    """读快照那一刻发布的事件，客户端也必须收到。"""
    pubsub = FakePubSub()
    state = {"terminal_checks": 0}

    def load_history(_):
        # 模拟"正在读快照时，worker 发布了一条 node_finished"
        pubsub.publish(json.dumps({"type": "node_finished", "node_id": "A"}))
        return [{"type": "workflow_state", "status": "RUNNING"}]

    def is_terminal(_):
        state["terminal_checks"] += 1
        return state["terminal_checks"] > 1

    async def collect():
        return [item async for item in event_stream(
            "wf-1",
            load_history=load_history,
            subscribe=lambda ch: pubsub.subscribe(ch),
            is_terminal=is_terminal,
            read_message=read_from,
        )]

    events = asyncio.run(collect())

    assert [json.loads(e["data"])["type"] for e in events] == [
        "workflow_state",
        "node_finished",
    ]


def test_event_stream_subscribes_to_given_channel():
    """泛化：workflow 级订阅 workflow 频道，不再是写死的 task 频道。"""
    channels: list[str] = []
    pubsub = FakePubSub()

    async def collect():
        return [item async for item in event_stream(
            "wf-1",
            load_history=lambda _: [],
            subscribe=lambda ch: channels.append(ch) or pubsub.subscribe(ch),
            is_terminal=lambda _: True,
            channel="workflow:wf-1:events",
        )]

    asyncio.run(collect())

    assert channels == ["workflow:wf-1:events"]


# ---------------------------------------------------------------------------
# H2：快照折叠（纯函数）
# ---------------------------------------------------------------------------

def test_build_snapshot_starts_with_workflow_state():
    workflow = {"id": "wf-1", "goal": "调研新能源", "status": "RUNNING"}

    events = build_snapshot(workflow, [])

    assert events[0]["type"] == "workflow_state"
    assert events[0]["workflow_id"] == "wf-1"
    assert events[0]["goal"] == "调研新能源"
    assert events[0]["status"] == "RUNNING"


def test_build_snapshot_folds_latest_attempt_per_node():
    """节点重试过：只发最新一次尝试的状态，attempt 记录总共试了几次。"""
    workflow = {"id": "wf-1", "goal": "调研", "status": "RUNNING"}
    tasks = [
        {"node_id": "A", "status": "FAILED", "answer": "第一次挂了"},
        {"node_id": "A", "status": "COMPLETED", "answer": "第二次成功"},
    ]

    node_events = [e for e in build_snapshot(workflow, tasks) if e["type"] == "node_state"]

    assert len(node_events) == 1                  # 两条 task 行 → 只发一条
    assert node_events[0]["node_id"] == "A"
    assert node_events[0]["status"] == "COMPLETED"
    assert node_events[0]["attempt"] == 2
    assert node_events[0]["answer"] == "第二次成功"


def test_build_snapshot_sorts_nodes_and_skips_legacy_rows():
    workflow = {"id": "wf-1", "goal": "调研", "status": "RUNNING"}
    tasks = [
        {"node_id": "C", "status": "PENDING", "answer": None},
        {"node_id": None, "status": "COMPLETED", "answer": "阶段 4 的老任务"},
        {"node_id": "A", "status": "COMPLETED", "answer": "A 的产出"},
        {"node_id": "B", "status": "RUNNING", "answer": None},
    ]

    node_events = [e for e in build_snapshot(workflow, tasks) if e["type"] == "node_state"]

    assert [e["node_id"] for e in node_events] == ["A", "B", "C"]   # 顺序稳定
    assert node_events[0]["answer"] == "A 的产出"
    assert node_events[0]["attempt"] == 1


def test_build_snapshot_with_terminal_workflow_and_failed_node():
    workflow = {"id": "wf-2", "goal": "测试终态", "status": "FAILED"}
    tasks = [
        {"node_id": "X", "status": "FAILED", "answer": None},
        {"node_id": "X", "status": "FAILED", "answer": None},
        {"node_id": "X", "status": "FAILED", "answer": None},
    ]

    events = build_snapshot(workflow, tasks)

    # Check workflow_state
    assert events[0]["type"] == "workflow_state"
    assert events[0]["workflow_id"] == "wf-2"
    assert events[0]["goal"] == "测试终态"
    assert events[0]["status"] == "FAILED"

    # Check node_state for node X
    node_events = [e for e in events if e["type"] == "node_state"]
    assert len(node_events) == 1
    assert node_events[0]["node_id"] == "X"
    assert node_events[0]["status"] == "FAILED"
    assert node_events[0]["attempt"] == 3
