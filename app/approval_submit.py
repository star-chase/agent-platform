"""审批决策服务（阶段 6-C）：把"人类点了批准/拒绝"变成"系统继续/终止"。

和 task_submit / workflow_submit 同一个模式：HTTP 层只做参数校验和错误码映射，
真正的逻辑放这里，用临时库 + 假的入队函数就能离线测透。

为什么"决策"和"入队"是两步：
    decide_approval 负责 **数据库事实**（审批记录 + 任务状态 + workflow 状态）；
    enqueue 负责 **消息投递**（可能失败、可能重复）。
    先落库、后投递，是 at-least-once 的基本要求：投递失败至少状态是对的，
    可以靠对账/重投补齐；反过来（先投递再落库）一旦事务回滚，
    任务就会"凭空跑起来而没有任何记录"。
"""

from typing import Any, Callable

from app.approvals import decide_approval


def decide_and_enqueue(
    approval_id: str,
    approved: bool,
    comment: str | None = None,
    decided_by: str | None = None,
    db_path: str | None = None,
    enqueue: Callable[[str], Any] | None = None,
) -> dict:
    """处理一次人类决策；批准时把任务重新入队。

    Returns:
        decide_approval 的返回值（approval / task_id / task_status / should_enqueue）

    顺序很重要：**先落库（decide_approval）、后投递（enqueue）**。
    反过来一旦事务回滚，任务就会"凭空跑起来而系统里没有任何记录"。
    """
    result = decide_approval(db_path, approval_id, approved, comment, decided_by)
    if result["should_enqueue"]:
        if enqueue is None:
            raise RuntimeError(
                f"审批 {approval_id} 已批准，但 enqueue 是 None，部署配置漏了"
            )
        enqueue(result["task_id"])
    return result
