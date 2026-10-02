"""Workflow 调度器：按 DAG 依赖把子任务派发给 worker。

设计原则：把"纯决策"和"IO（查库/建行/入队）"分开。
5-D-1 负责纯决策（本文件）；IO 在 5-D-2（存储层）和 5-D-3（主循环）里补上。
"""

from app.dag import DAG


# 单个上游输出注入 prompt 时的最大字符数（上下文窗口管理的落地）。
MAX_UPSTREAM_CHARS = 2000


def dispatch_ready(
    dag: DAG,
    completed: set[str],
    dispatched: set[str],
    retryable: set[str] | None = None,
) -> list[str]:
    """返回本轮应该派发的节点 id 列表。

    条件：依赖已全部完成、自身未完成、且（还没派发过 或 属于可重试节点）。

    为什么必须排除 dispatched：节点派发出去后任务处于 PENDING/RUNNING，
    既不在 completed 里、ready_nodes 又会再次返回它。只看 completed 会导致
    每轮调度都重复建 task、重复执行、重复烧 token。

    为什么还需要 retryable（5-G 节点级失败重试）：
    节点失败后要重派，会给它**新建**一条 task 行，可这个 node_id 已经在
    dispatched 里了。所以调度器必须能区分两种"已在 dispatched 里"的节点：
        - 还在跑 / 已经跑完的 → 不能重派；
        - 失败了、但重试次数还没用完的 → 必须重派。

    Args:
        dag: 整个 workflow 的 DAG。
        completed: 最新一次尝试 COMPLETED 的节点 id 集合。
        dispatched: 已经建过 task 行（PENDING/RUNNING/COMPLETED/FAILED）的节点 id 集合。
        retryable: 允许"重派"的节点 id 集合（失败且未达重试上限的）。
    """
    retryable = retryable or set()
    ready = dag.ready_nodes(completed)
    # 已派发的默认跳过；只有"失败且还有重试额度"的节点才允许重派
    return [node_id for node_id in ready if node_id not in dispatched or node_id in retryable]


def build_node_question(
    dag: DAG,
    node_id: str,
    upstream_outputs: dict[str, str],
    max_chars: int = MAX_UPSTREAM_CHARS,
) -> str:
    """把节点 description + 直接上游节点的输出，拼成这个子任务的 question。

    只取 dag.dependencies[node_id] 里的**直接**上游（"上游的上游"不重复注入）；
    某个上游输出超过 max_chars 时截断并标注，避免把上下文窗口撑爆。

    Args:
        upstream_outputs: 节点 id -> 该节点任务的最终答案。没有的节点会被跳过。
    """
    description = dag.nodes[node_id].get("description", "")
    question = f"任务：{description}"
    sections = []
    for dependency in sorted(dag.dependencies[node_id]):
        output = upstream_outputs.get(dependency)
        if not output:
            continue
        if len(output) > max_chars:
            output = output[:max_chars] + "…（已截断）"
        sections.append(f"【{dependency}】\n{output}")
    if sections:
        question += "\n\n上游任务结果：\n" + "\n".join(sections)
    return question
