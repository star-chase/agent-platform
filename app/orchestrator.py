"""Workflow 编排进程（阶段 5-D-4）。

三个角色的分工，这是本阶段最重要的架构认知：
    API        —— 接收目标、调 Planner 规划、落库，**不跑长循环**
    编排器      —— 轮询 RUNNING 的 workflow，按 DAG 派发子任务（本文件）
    worker     —— 从队列取单个 task 执行，不知道 DAG 全局长什么样

为什么编排要单独一个进程？
1. API 进程里跑长循环会占用请求处理资源（阶段 1 思考题 6 的 async 阻塞问题同源）；
2. 多副本部署 API 时，每个副本都会跑一份调度 → **重复派发**；
3. 编排需要"全局视角"（整个 DAG + 所有节点状态），这和"执行单个任务"是两种关注点。
"""

import logging
import time
from typing import Any, Callable

from app.config import get_settings
from app.logging_setup import log_context
from app.planner import plan_to_dag
from app.workflow_runner import (
    COMPLETED,
    DISPATCHED,
    FAILED,
    WAITING,
    WorkflowScheduler,
)


# 用 __spec__.name 而不是 __name__：这两个进程都是用 `python -m app.xxx` 启动的，
# 那种情况下 __name__ 是 "__main__"，日志里的 logger 字段就变成 "__main__" ——
# worker 和 orchestrator 会**长得一模一样**，按 logger 过滤/分派日志全部失效。
# __spec__.name 拿到的永远是真实模块名（PEP 451）；普通 import 时两者相同。
logger = logging.getLogger(__spec__.name if __spec__ else __name__)


class WorkflowOrchestrator:
    """轮询未完成的 workflow，把它们往前推进。"""

    def __init__(
        self,
        store: Any,
        enqueue: Callable[[str], None],
        poll_interval: float = 2.0,
        dag_loader: Callable[[str], Any] | None = None,
        should_stop: Callable[[], bool] | None = None,
        recover_stale: Callable[[], Any] | None = None,
        publish: Callable[[str, dict[str, Any]], None] | None = None,
        request_approval: Callable[[str, str, str, str | None], None] | None = None,
    ):
        self.store = store
        self.enqueue = enqueue
        # 阶段 6-B：需要审批的节点在派发时调它建审批记录；不注入时调度器会对
        # "要求审批的节点"报错（fail closed，绝不静默放行）。
        self.request_approval = request_approval
        self.poll_interval = poll_interval
        # dag_loader 也做成可注入的：默认从库里读 dag JSON 再重建成 DAG 对象
        self.dag_loader = dag_loader or self._load_dag
        self.should_stop = should_stop or (lambda: False)
        # 每轮先回收僵尸任务（卡在 RUNNING 太久的），否则 workflow 会永久卡住
        self.recover_stale = recover_stale
        # 可注入的"发布 workflow 级事件"函数（编排器掌握全局状态，是终态事件的发布方）；
        # 离线测试不注入就是 None，所以调用点必须判空。
        self.publish = publish

    def _load_dag(self, workflow_id: str):
        """从数据库读回 dag JSON，用 plan_to_dag 重新校验并重建 DAG 对象。"""
        workflow = self.store.get_workflow(workflow_id)
        if workflow is None:
            raise KeyError(f"workflow 不存在：{workflow_id}")
        return plan_to_dag(workflow["dag"])

    def run_once(self) -> list[tuple[str, str]]:
        """对每个 RUNNING 的 workflow 各 tick 一次。

        Returns:
            本轮所有"非 WAITING"的 (workflow_id, tick 结果) 列表，
            例如 [("wf-1", "DISPATCHED"), ("wf-2", "COMPLETED")]。
            WAITING 表示没什么可做，不算推进。
        """
        if self.recover_stale:
            self.recover_stale()
        advance = []
        for workflow_id in self.store.list_workflow_ids_by_status("RUNNING"):
            dag = self.dag_loader(workflow_id)
            scheduler = WorkflowScheduler(
                self.store, dag, self.enqueue,
                poll_interval=0, request_approval=self.request_approval,
            )
            status = scheduler.tick(workflow_id)

            # 节点级事件：本轮派发了谁就先告诉前端（否则用户要干等到节点跑完才有动静）。
            # 重派（节点级重试）也走这里——同一个 node_id 会再次出现，前端按幂等处理。
            if self.publish:
                # 绑上 workflow_id：本轮所有事件日志都自动带上它。
                # 注意 last_dispatched / last_waiting_approval 是 list[str]（只有 node_id），
                # 调度器并没有把它们做成 {node_id: task_id} 的字典 —— 所以这里记不到 task_id。
                # 想要 task_id 就得改调度器的返回结构，5 条测试断言要跟着改；
                # 排障时 workflow_id + node_id 已经足够定位，不值得付这个价。
                with log_context(workflow_id=workflow_id):
                    for node_id in scheduler.last_dispatched:
                        self.publish(workflow_id, {
                            "type": "node_dispatched",
                            "node_id": node_id,
                            "status": "PENDING",
                        })
                        logger.info("node_dispatched", extra={
                            "node_id": node_id,
                            "status": "PENDING",
                        })
                    # 挂起等审批的节点单独发事件：否则"等人批"的 workflow 在前端看起来和"卡死"一样
                    for node_id in scheduler.last_waiting_approval:
                        self.publish(workflow_id, {
                            "type": "node_waiting_approval",
                            "node_id": node_id,
                            "status": "WAITING_APPROVAL",
                        })
                        logger.info("node_waiting_approval", extra={
                            "node_id": node_id,
                            "status": "WAITING_APPROVAL",
                        })

            if status != WAITING:
                advance.append((workflow_id, status))
                if status in {"COMPLETED", "FAILED"}:
                    # 日志和事件分开判：self.publish 可能是 None（离线测试没注入），
                    # 但"流水线走到终态"这件事**无论有没有订阅者都该留痕**。
                    logger.info("workflow_finished", extra={"status": status})
                    # workflow 级终态事件
                if status in {"COMPLETED", "FAILED"} and self.publish:
                    self.publish(workflow_id, {
                        "type": "workflow_finished",
                        "workflow_id": workflow_id,
                        "status": status,
                    })
        return advance

    def run_forever(self) -> None:
        """持续编排，直到 should_stop() 返回 True。"""
        while not self.should_stop():
            self.run_once()
            time.sleep(self.poll_interval)


def main() -> None:
    """组装真实依赖并启动编排进程：python -m app.orchestrator"""
    import os

    from dotenv import load_dotenv

    from app.approvals import create_approval_request
    from app.events import publish_workflow_event
    from app.queue import enqueue_task, get_redis_client
    from app.config import describe_settings
    from app.logging_setup import setup_logging
    from app.recovery import recover_stale_tasks
    from app.workflow_store import SQLiteWorkflowStore

    load_dotenv()
    # 编排进程的日志是排查"workflow 为什么卡住"的第一现场：
    # 谁被派发了、谁在等审批、哪个节点把整条流水线拖死了。
    settings = get_settings()
    # ⚠️ 顺序：**先配置日志，再打第一条日志**。
    # 反过来的话，第一条日志（恰好是最有用的那条"启动快照"）根本进不了 handler ——
    # 它会被 root logger 的 lastResort 丢到 stderr，而你正把 stderr 重定向到 /dev/null，
    # 于是"启动快照"变成了"什么都没有"。（这条是实测踩出来的，不是理论。）
    setup_logging(log_path=settings.log_path)
    # 启动时把配置打出来（不含密钥）：排障第一个问题永远是"这个进程到底连的哪个库"。
    logger.info("orchestrator_starting", extra=describe_settings(settings))
    db_path = settings.database_path
    redis_client = get_redis_client()
    store = SQLiteWorkflowStore(db_path)
    orchestrator = WorkflowOrchestrator(
        store,
        enqueue=lambda task_id: enqueue_task(task_id, client=redis_client),
        poll_interval=settings.orchestrator_poll_interval,
        recover_stale=lambda: recover_stale_tasks(db_path),
        publish=lambda workflow_id, event: publish_workflow_event(
            workflow_id, event, client=redis_client
        ),
        request_approval=lambda workflow_id, node_id, task_id, reason: create_approval_request(
            db_path, task_id, workflow_id, node_id, reason
        ),
    )
    orchestrator.run_forever()


if __name__ == "__main__":
    main()
