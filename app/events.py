from app.config import get_settings
import json
from datetime import datetime, timezone
from typing import Any


def event_channel(task_id: str) -> str:
    """返回某个任务的 Redis Pub/Sub 频道名。"""
    return f"task:{task_id}:events"


def get_redis_client(url: str | None = None) -> Any:
    """创建 Redis 客户端；实际连接在第一次命令执行时发生。"""
    import redis

    return redis.Redis.from_url(
        url or get_settings().redis_url,
        decode_responses=True,
    )


def publish_event(task_id: str, event: dict[str, Any], client: Any = None) -> None:
    """向任务频道发布一条 JSON 事件（子任务级进度）。"""
    client = client or get_redis_client()
    payload = dict(event)
    payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
    client.publish(event_channel(task_id), json.dumps(payload, ensure_ascii=False))


def workflow_channel(workflow_id: str) -> str:
    """返回某个 workflow 的 Redis Pub/Sub 频道名（workflow 级进度）。"""
    return f"workflow:{workflow_id}:events"


def publish_workflow_event(workflow_id: str, event: dict[str, Any], client: Any = None) -> None:
    """向 workflow 频道发布一条 JSON 事件（节点完成、workflow 终态等）。

    两个频道是**两层进度**：
        task 级     —— 一个 Agent 内部的每一步（阶段 4 已有）
        workflow 级 —— 整个目标的状态 + 每个节点跑到哪了（阶段 5-H）
    """
    client = client or get_redis_client()
    payload = dict(event)
    payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
    client.publish(workflow_channel(workflow_id), json.dumps(payload, ensure_ascii=False))
