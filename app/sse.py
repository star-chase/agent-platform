import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, Callable

from app.events import event_channel


async def event_stream(
    resource_id: str,
    load_history: Callable[[str], list[dict[str, Any]]],
    subscribe: Callable[[str], Any],
    is_terminal: Callable[[str], bool],
    read_message: Callable[[Any], Any] | None = None,
    channel: str | None = None,
) -> AsyncIterator[dict[str, str]]:
    """先发快照（历史事件），再持续发实时事件。

    阶段 5-H 泛化成"任何有快照 + 有实时事件的资源"：用 channel 指定订阅哪个频道，
    task 级（/tasks/{id}/events）和 workflow 级（/workflows/{id}/events）共用这一套。

    两个关键设计（都是踩过坑才定下来的）：

    1. **先订阅、再读快照**。Redis Pub/Sub 不保存历史，订阅之前发布的消息永远收不到；
       如果先读快照再订阅，两者之间就有一段"空窗"——这期间发布的事件既不在快照里
       （快照已读完），也不会被订阅收到，客户端会**永远少看到一步**。
       先订阅之后，空窗期的消息缓存在 pubsub 对象里，进循环时依次读出。
       代价是可能收到"快照里已经有"的重复事件，所以状态类事件必须做成**幂等**的。

    2. **subscribe 拿到资源后立刻进 try/finally**。释放范围必须包住"从获取资源那一刻起"
       的全部出口——包括"读完快照发现已终态，直接 return"这条最常见的路径，
       否则每个终态请求都会泄漏一个 Redis 连接。
    """
    channel = channel or event_channel(resource_id)
    subscription = subscribe(channel)
    try:
        for item in load_history(resource_id):
            yield {"event": item.get("type", "step"), "data": json.dumps(item, ensure_ascii=False)}

        # 快照发完就已经是终态：不会有新事件了，直接收尾（finally 会关掉订阅）
        if is_terminal(resource_id):
            return

        while True:
            if read_message is not None:
                message = await read_message(subscription)
            else:
                # get_message 是**同步阻塞**调用（最长等 timeout 秒），必须丢到线程里，
                # 否则会卡住整个事件循环——阶段 1 思考题 6 的同一个坑。
                message = await asyncio.to_thread(
                    subscription.get_message,
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
            if not message:
                if is_terminal(resource_id):
                    break
                continue
            data = message.get("data", message)
            event = json.loads(data) if isinstance(data, str) else data
            # 事件名透传事件自身的 type：前端靠 SSE 的 event 字段区分两层进度
            yield {"event": event.get("type", "step"), "data": json.dumps(event, ensure_ascii=False)}
            if is_terminal(resource_id):
                break
    finally:
        close = getattr(subscription, "close", None)
        if close:
            close()
