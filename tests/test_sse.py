import asyncio
import json
import threading

from app.sse import event_stream


class FakeSubscription:
    """假订阅：记录关闭状态，以及 get_message 是在哪个线程里被调用的。"""

    def __init__(self, messages):
        self.messages = list(messages)
        self.closed = False
        self.thread_ids: list[int] = []

    def get_message(self, **kwargs):
        self.thread_ids.append(threading.get_ident())
        if self.messages:
            return {"data": self.messages.pop(0)}
        return None

    def close(self):
        self.closed = True


def test_event_stream_sends_history_then_live_events():
    subscription = FakeSubscription([json.dumps({"type": "step_completed", "iteration": 2})])
    terminal_checks = {"count": 0}

    def is_terminal(_):
        terminal_checks["count"] += 1
        return terminal_checks["count"] > 1

    async def collect():
        async def read_message(source):
            return source.get_message(ignore_subscribe_messages=True, timeout=1.0)

        return [item async for item in event_stream(
            "task-1",
            load_history=lambda _: [{"type": "step_completed", "iteration": 1}],
            subscribe=lambda _: subscription,
            is_terminal=is_terminal,
            read_message=read_message,
        )]

    events = asyncio.run(collect())

    assert len(events) == 2
    assert json.loads(events[0]["data"])["iteration"] == 1
    assert json.loads(events[1]["data"])["iteration"] == 2
    # SSE 的事件名必须透传事件自身的 type：前端靠它区分
    # step_completed（task 级）/ node_finished / workflow_finished（workflow 级）
    assert events[0]["event"] == "step_completed"
    assert events[1]["event"] == "step_completed"
    assert subscription.closed is True


def test_terminal_resource_still_closes_subscription():
    """终态资源：仍然要先订阅（保证空窗不丢事件），但必须立刻关掉订阅。

    这条测试的前身断言的是"终态就不订阅"——5-H 修订阅空窗后旧行为不再成立。
    但"不能泄漏 Redis 连接"这条约束更强，所以断言从"有没有订阅"换成了
    "订阅有没有被关闭"：测试名也跟着改，别让名字和断言互相矛盾。
    """
    subscription = FakeSubscription([])
    subscribed: list[str] = []

    async def collect():
        return [item async for item in event_stream(
            "task-1",
            load_history=lambda _: [{"type": "task_completed"}],
            subscribe=lambda channel: subscribed.append(channel) or subscription,
            is_terminal=lambda _: True,
        )]

    events = asyncio.run(collect())

    assert subscribed == ["task:task-1:events"]   # 先订阅了（订阅空窗已修）
    assert len(events) == 1                       # 但只发快照，不等实时事件
    assert subscription.closed is True            # 而且没有泄漏连接


def test_event_stream_waits_for_redis_without_blocking_event_loop():
    """Redis 的 get_message 是同步阻塞调用，必须丢到线程里等。

    直接在 async 函数里调用它会卡住整个事件循环（阶段 1 思考题 6 的同一个坑），
    表现为"一个 SSE 连接在等，同进程其他请求全部排队"。
    """
    subscription = FakeSubscription([])
    state = {"done": False}

    async def collect():
        return [item async for item in event_stream(
            "task-1",
            load_history=lambda _: [],
            subscribe=lambda _: subscription,
            is_terminal=lambda _: state["done"],
        )]

    async def main():
        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)            # 让 get_message 至少被调用一次
        state["done"] = True
        return await task

    asyncio.run(main())

    assert subscription.thread_ids, "get_message 一次都没被调用，测试无效"
    assert all(tid != threading.get_ident() for tid in subscription.thread_ids), (
        "get_message 跑在主线程上 —— 说明没用 asyncio.to_thread，会阻塞事件循环"
    )
