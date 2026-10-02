from datetime import datetime, timezone


# 阶段 6 的审批边：
#   批准 → PENDING（只是解除了阻塞，任务**还没开始执行**，要重新入队）
#   拒绝 → CANCELLED（人类的否决是确定性事件，不走重试）
#   动态审批（6-D）允许 RUNNING 中途停下来等人，恢复时回 RUNNING
# 阶段 7-A-2 的预算边：RUNNING -> BUDGET_EXCEEDED（终态，没有出边）。
# 为什么它不是 FAILED：FAILED 有出边（-> RUNNING，节点级重试要用），
# 而"预算已经花光"重试一次还是超支，是确定性失败。**状态的出边形状本身就是语义**。
VALID_TRANSITIONS = {
    "PENDING": {"RUNNING", "CANCELLED", "WAITING_APPROVAL"},
    "RUNNING": {"COMPLETED", "FAILED", "CANCELLED", "WAITING_APPROVAL", "BUDGET_EXCEEDED"},
    "FAILED": {"RUNNING"},
    "WAITING_APPROVAL": {"PENDING", "RUNNING", "CANCELLED"},
    "COMPLETED": set(),
    "CANCELLED": set(),
    "BUDGET_EXCEEDED": set(),
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
