"""人工审批服务层（阶段 6，Human-in-the-Loop）。

为什么需要：有些步骤不能全自动（发邮件、花钱、删数据、对外发布报告）。

技术本质 = 阶段 3 的断点续跑 + 一个等待状态：

    Task:     PENDING -> WAITING_APPROVAL -> PENDING（批准，重新入队）
                                         -> CANCELLED（拒绝）
    Workflow: 拒绝 -> FAILED（人类的否决是确定性事件，**不该走节点级重试**）

两条贯穿本阶段的设计原则：

1. **等待不占资源**：审批状态只存在于数据库，worker 绝不 sleep 等人类
   （人可能几小时才回，4 个 worker 全在等审批 = 系统瘫痪）；
2. **人类意见必须进入 Agent 的上下文**：只改数据库状态的话，Agent 不知道自己被审批过，
   也不知道人类提了什么要求（比如「批准，但金额改成 100」）。

分层约定（沿用 task_submit / workflow_submit 的模式）：
本文件**不 import Redis、不 import FastAPI**——入队由调用方（HTTP 层）负责，
所以这里可以用临时库离线测透。
"""

import logging
import uuid

from app.db import connect


logger = logging.getLogger(__name__)


APPROVAL_STATUSES = {"PENDING", "APPROVED", "REJECTED"}


class ApprovalError(ValueError):
    """审批相关业务错误的基类。"""


class ApprovalNotFoundError(ApprovalError):
    def __init__(self, approval_id: str):
        super().__init__(f"审批记录不存在：{approval_id}")


class ApprovalAlreadyDecidedError(ApprovalError):
    def __init__(self, approval_id: str, status: str):
        super().__init__(f"审批 {approval_id} 已经处理过了（当前状态 {status}），不能重复决策")


def create_approval_request(
    db_path: str | None,
    task_id: str,
    workflow_id: str | None = None,
    node_id: str | None = None,
    reason: str | None = None,
) -> str:
    """创建一条待审批记录（派发节点时调用），返回 approval_id。"""
    approval_id = str(uuid.uuid4())
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO approvals (id, task_id, workflow_id, node_id, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (approval_id, task_id, workflow_id, node_id, reason),
        )
        conn.commit()
    finally:
        conn.close()
    # 用 WARNING：这是"流水线卡在等人"的信号，全项目最该被人看见的就是它 ——
    # 没接前端的人只能靠日志发现"有活在等批准"。
    logger.warning("approval_requested", extra={
        "approval_id": approval_id,
        "node_id": node_id,
        "reason": reason,
    })
    return approval_id


def list_pending_approvals(db_path: str | None = None) -> list[dict]:
    """列出全部待审批项（按创建时间升序），给审批人和前端用。

    每条要带足够信息让人类做决定：approval_id / task_id / workflow_id / node_id /
    reason / created_at，外加这个任务的问题原文（tasks.question）。

    JOIN tasks 取问题原文；只取 status = 'PENDING' 的，按创建顺序返回。
    """
    conn = connect(db_path, row_factory=True)
    try:
        rows = conn.execute(
            "SELECT a.id, a.task_id, a.workflow_id, a.node_id, "
            "a.reason, a.created_at, t.question "
            "FROM approvals AS a "
            "JOIN tasks AS t ON a.task_id = t.id "
            "WHERE a.status = 'PENDING' "
            "ORDER BY a.created_at ASC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def decide_approval(
    db_path: str | None,
    approval_id: str,
    approved: bool,
    comment: str | None = None,
    decided_by: str | None = None,
) -> dict:
    """人类做出决定（批准 / 拒绝），返回本次决策的结果。

    返回：{"approval": {...}, "task_id": ..., "task_status": ..., "should_enqueue": bool}
    should_enqueue 交给 HTTP 层去真正入队（服务层不碰 Redis）。

    必须做完这几件事：
      1) 审批记录 PENDING -> APPROVED / REJECTED，写 comment / decided_by / decided_at
         - 记录不存在      -> raise ApprovalNotFoundError
         - 状态不是 PENDING -> raise ApprovalAlreadyDecidedError（不能重复决策）
      2) **批准**：task 从 WAITING_APPROVAL -> PENDING，并把人类意见追加进 question，
         形如：「...原问题...\n\n【人工审批】已批准，意见：金额改成 100」
         （没意见时就是「【人工审批】已批准。」）-> should_enqueue = True
      3) **拒绝**：task -> CANCELLED（理由写进 answer），workflow -> FAILED
         （人类否决是确定性事件，**不走重试**）-> should_enqueue = False
      4) 最后返回上面那个 dict（approval 用最新读出来的整行）。

    实现顺序：先读 approval 行 -> 判空/判状态 -> 一个事务里做完三处更新 -> 提交后再读回来返回。
    """
    conn = connect(db_path, row_factory=True)
    try:
        # 1) 读 approval 行
        approval_row = conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        if not approval_row:
            raise ApprovalNotFoundError(approval_id)
        if approval_row["status"] != "PENDING":
            raise ApprovalAlreadyDecidedError(approval_id, approval_row["status"])

        # 2) 开事务做三处更新
        task_id = approval_row["task_id"]
        workflow_id = approval_row["workflow_id"]
        comment_text = comment or ""
        decided_by_text = decided_by or ""
        # 时间统一：decided_at 交给数据库写 —— SQLite 的 datetime('now') 是 UTC，
        # 且与同一张表的 created_at 格式完全一致（'YYYY-MM-DD HH:MM:SS'）。
        # 不要在 Python 侧自己拼时间字符串：本地时区、格式、精度都会各写各的。
        # （阶段 8-0 换 PostgreSQL 时，这一步变成 NOW() / timestamptz。）

        if approved:
            # 批准：更新审批记录和任务状态
            conn.execute(
                "UPDATE approvals SET status = 'APPROVED', comment = ?, "
                "decided_by = ?, decided_at = datetime('now') WHERE id = ?",
                (comment_text, decided_by_text, approval_id),
            )
            # 更新任务状态为 PENDING，并追加人类意见到 question
            task_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            new_question = (
                f"{task_row['question']}\n\n【人工审批】已批准，意见：{comment_text}"
                if comment_text
                else f"{task_row['question']}\n\n【人工审批】已批准。"
            )
            conn.execute(
                "UPDATE tasks SET status = 'PENDING', question = ? WHERE id = ?",
                (new_question, task_id),
            )
            new_task_status = "PENDING"
            should_enqueue = True
        else:
            # 拒绝：更新审批记录、任务状态为 CANCELLED、工作流状态为 FAILED
            conn.execute(
                "UPDATE approvals SET status = 'REJECTED', comment = ?, "
                "decided_by = ?, decided_at = datetime('now') WHERE id = ?",
                (comment_text, decided_by_text, approval_id),
            )
            conn.execute(
                "UPDATE tasks SET status = 'CANCELLED', answer = ? WHERE id = ?",
                (f"【人工审批】已拒绝，理由：{comment_text}", task_id),
            )
            if workflow_id:
                conn.execute(
                    "UPDATE workflows SET status = 'FAILED' WHERE id = ?",
                    (workflow_id,),
                )
            new_task_status = "CANCELLED"
            should_enqueue = False

        conn.commit()
        logger.info("approval_decided", extra={
            "approval_id": approval_id,
            "node_id": approval_row["node_id"],
            "approved": approved,
            "task_status": new_task_status,
        })

        # 决策后再读回来：返回给调用方的必须是"落库后的真实状态"，而不是我们以为的状态
        updated_approval = conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        return {
            "approval": dict(updated_approval),
            "task_id": task_id,
            "task_status": new_task_status,
            "should_enqueue": should_enqueue,
        }
    finally:
        conn.close()