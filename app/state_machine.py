from datetime import datetime, timezone


VALID_TRANSITIONS = {
    "PENDING": {"RUNNING", "CANCELLED"},
    "RUNNING": {"COMPLETED", "FAILED", "CANCELLED"},
    "FAILED": {"RUNNING"},
    "COMPLETED": set(),
    "CANCELLED": set(),
}


class InvalidTransitionError(ValueError):
    """当状态机收到不允许的状态转移时抛出。"""


def transition(entity: dict, new_status: str) -> dict:
    """校验并执行一次状态转移。

    entity 至少需要包含 status 字段；函数会原地更新 entity，
    同时返回 entity，方便调用方继续使用。
    """
    current_status = entity.get("status")
    allowed_statuses = VALID_TRANSITIONS.get(current_status)
    if allowed_statuses is None or new_status not in allowed_statuses:
        raise InvalidTransitionError(
            f"不允许的状态转移：{current_status} -> {new_status}"
        )
    entity["status"] = new_status
    entity["updated_at"] = datetime.now(timezone.utc).isoformat()
    return entity
