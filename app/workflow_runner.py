"""Workflow 调度主循环（阶段 5-D-3）——全项目最核心的一段。

核心思路：调度器只做"决策 + 记账"，执行永远交给 worker。每一轮：
    读状态 → 判终态 → 算出该派发谁 → 建 task 行 + 入队 → 等下一轮

依赖注入：store（存储层）和 enqueue（入队函数）都从外面传进来，
所以这个文件可以完全不连数据库、不连 Redis 就测透
（和阶段 4-C 给执行器注入 on_step 是同一个解耦思路）。
"""

import time
from typing import Any, Callable

from app.dag import DAG
from app.scheduler import build_node_question, dispatch_ready


# tick() 的四种返回值
COMPLETED = "COMPLETED"
FAILED = "FAILED"
DISPATCHED = "DISPATCHED"
WAITING = "WAITING"


def sink_nodes(dag: DAG) -> list[str]:
    """末端节点：不被任何其他节点依赖的节点，它们的输出就是 workflow 的最终产出。"""
    depended_on: set[str] = set()
    for dependencies in dag.dependencies.values():
        depended_on |= dependencies
    return sorted(node_id for node_id in dag.nodes if node_id not in depended_on)


def collect_final_output(dag: DAG, outputs: dict[str, str]) -> str:
    """把末端节点的输出拼成 workflow 的 final_output。"""
    return "\n\n".join(
        outputs[node_id] for node_id in sink_nodes(dag) if node_id in outputs
    )


class WorkflowScheduler:
    """按 DAG 依赖派发子任务，并跟踪 workflow 直到终态。"""

    def __init__(
        self,
        store: Any,
        dag: DAG,
        enqueue: Callable[[str], None],
        poll_interval: float = 2.0,
        max_ticks: int | None = None,
        max_node_attempts: int = 3,
        request_approval: Callable[[str, str, str, str | None], None] | None = None,
    ):
        self.store = store
        self.dag = dag
        self.enqueue = enqueue
        # 阶段 6-B：需要人工审批的节点，派发时调它建审批记录（参数：workflow_id/node_id/task_id/reason）。
        # 和 enqueue 一样用注入：调度器只做决策，写数据库 / 发事件都不是它的职责。
        self.request_approval = request_approval
        self.poll_interval = poll_interval
        self.max_ticks = max_ticks
        # 单个节点最多尝试几次（含第一次）。3 = 失败后最多再重试 2 次。
        if max_node_attempts < 1:
            raise ValueError("max_node_attempts 必须 >= 1")
        # 本轮 tick 实际派发了哪些节点。调度器只做决策，"发事件"不是它的职责，
        # 但它必须把决策结果暴露出来——编排进程靠这个给前端发 node_dispatched。
        self.last_dispatched: list[str] = []
        # 本轮因为"需要人工审批"而挂起（未入队）的节点，编排进程靠它发 node_waiting_approval
        self.last_waiting_approval: list[str] = []
        self.max_node_attempts = max_node_attempts

    def tick(self, workflow_id: str) -> str:
        """执行一轮调度，返回本轮结果：COMPLETED / FAILED / DISPATCHED / WAITING。

        判定顺序不能调换：先判"全部完成"再判"有失败"。
        因为实现节点级重试后，同一个节点可能同时存在 FAILED 和 COMPLETED 两条
        task 行——先判失败就会把已经全部跑完的 workflow 误判为失败。
        """
        state = self.store.get_scheduling_state(workflow_id)
        self.last_dispatched = []          # 每轮先清空，避免上一轮的结果被误读

        if set(self.dag.nodes) <= state["completed"]:
            final_output = collect_final_output(self.dag, state["outputs"])
            self.store.update_workflow_status(workflow_id, COMPLETED, final_output)
            return COMPLETED

        # 阶段 7-A-2：确定性失败优先判死 —— 有节点预算超支（fatal）就整条 workflow FAILED。
        # 位置必须在"全部完成"之后、"重试"之前：
        #   放到"全部完成"之前 → 会把其实已经跑完的 workflow 判成失败（5-G 踩过顺序坑）；
        #   放到"重试"之后     → 超支的节点会被当成"还有额度"白重派几次。
        # fatal 与 failed 是两个集合：超支是**确定性**失败（重试一次还是超支），
        # 不能进 failed —— 那个集合的语义是"还有重试额度的候选池"。
        if state["fatal"]:
            self.store.update_workflow_status(workflow_id, FAILED)
            return FAILED

        # 失败节点分两类：还有重试额度的（重派）vs 额度用光的（整个 workflow 判死）
        retryable: set[str] = set()
        if state["failed"]:
            retryable = {
                node_id
                for node_id in state["failed"]
                if state["attempts"].get(node_id, 0) < self.max_node_attempts
            }
            exhausted = state["failed"] - retryable
            if exhausted:
                self.store.update_workflow_status(workflow_id, FAILED)
                return FAILED

        self.last_waiting_approval = []          # 每轮先清空（和 last_dispatched 同理）
        ready = dispatch_ready(self.dag, state["completed"], state["dispatched"], retryable=retryable)
        waiting: list[str] = []
        for node_id in ready:
            question = build_node_question(self.dag, node_id, state["outputs"])
            depends_on = sorted(self.dag.dependencies[node_id])
            upstream = {
                dep: state["outputs"][dep]
                for dep in depends_on
                if dep in state["outputs"]
            }
            # 审批门控：这个布尔值决定三件事 —— 建行的状态、要不要建审批记录、要不要入队。
            # 只读一次 DAG，避免三个分支各读一次写出不一致。
            requires_approval = bool(self.dag.nodes[node_id].get("requires_approval"))

            # 安全门（fail closed）：要求审批却没有处理器 = 部署漏配。
            # 注意是判「对象本身是不是 None」，不是判「调用它之后的返回值」——
            # 后者连调用都执行不了（TypeError: 'NoneType' object is not callable）。
            if requires_approval and self.request_approval is None:
                raise RuntimeError(
                    f"节点 {node_id} 要求人工审批，但调度器没有注入 request_approval"
                )

            # 需要审批的节点「一出生就在等人」：状态直接是 WAITING_APPROVAL
            task_id = self.store.create_node_task(
                workflow_id, node_id, question, depends_on,
                status="WAITING_APPROVAL" if requires_approval else "PENDING",
                upstream_outputs=upstream,
            )

            if requires_approval:
                # 走到这里 request_approval 一定不是 None（上面已经拦住了）
                self.request_approval(
                    workflow_id, node_id, task_id,
                    self.dag.nodes[node_id].get("description"),
                )
                waiting.append(node_id)      # 等人批准，**不入队**
            else:
                self.enqueue(task_id)        # 入队后 worker 才会执行

        # last_dispatched 只放「真正入队」的节点（编排器据此发 node_dispatched/PENDING 事件）；
        # 挂起等审批的节点走 last_waiting_approval，语义不能混。
        self.last_dispatched = [node_id for node_id in ready if node_id not in waiting]
        self.last_waiting_approval = waiting
        return DISPATCHED if ready else WAITING

    def run_until_terminal(self, workflow_id: str) -> str:
        """反复 tick 直到 workflow 进入终态，返回最终状态。"""
        ticks = 0
        while self.max_ticks is None or ticks < self.max_ticks:
            ticks += 1
            status = self.tick(workflow_id)
            if status in (COMPLETED, FAILED):
                return status
            # 必须睡：否则就是空转轮询，白烧 CPU 和数据库连接。
            time.sleep(self.poll_interval)
        raise RuntimeError(
            f"workflow {workflow_id} 超过 max_ticks={self.max_ticks} 轮仍未进入终态"
        )
