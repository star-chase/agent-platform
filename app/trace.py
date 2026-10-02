"""简易追踪（阶段 7-C）：可观测性三支柱的第三根。

    Metrics（7-A / 7-A-2）  → 花了多少钱
    Logging（7-B）          → 发生了什么
    Tracing（本节）         → **时间花在哪一步**

为什么不用 OpenTelemetry：对学习项目太重（要起 collector + Jaeger + 换埋点方式）。
这里用最小实现覆盖同一套概念，面试时可以对照着讲：

    trace_id  = workflow_id        （一条流水线 = 一条 trace）
    span      = 一次节点尝试（tasks 的一行）+ 一次 LLM 调用（steps 的一行）
    span 时长 = finished_at - started_at

三个真问题，也是本模块存在的理由：

1. **哪一步最慢？** 不是"哪个节点跑得久"这么简单 —— 一次节点尝试的墙钟时间里混着
   LLM 延迟、工具执行、排队。所以要同时给出 `duration_ms`（墙钟）和 `llm_ms`（纯模型延迟），
   两者的差就是"非 LLM 的时间"。只看一个数字永远说不清"该优化哪里"。
2. **哪一步 token 最多？** 钱的分布和时间的分布经常**不在同一个节点上**。
3. **关键路径是哪条？** "最慢的节点"只告诉你一个点；真正决定整条流水线墙钟时间的
   是**最长的那条依赖链**。把不在关键路径上的节点加速一倍，整条流水线一秒都不会变快 ——
   这是 DAG 调度的核心直觉，也是面试常问点。
"""

from datetime import datetime
from typing import Any, Iterable

from app.cost import estimate_cost
from app.db import connect

DEFAULT_MODEL = "deepseek-v4-flash"


def parse_ts(text: str | None) -> datetime | None:
    """解析库里的时间戳（'YYYY-MM-DD HH:MM:SS[.mmm]'，UTC，无时区后缀）。

    解析不了就返回 None（老数据是 NULL、也可能有人手改过）。
    **不要把解析失败当成 0** —— "不知道多久"和"瞬间完成"是两件事，
    混成 0 会让"最慢节点"的排名悄悄错掉。
    """
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def duration_ms(started_at: str | None, finished_at: str | None) -> int | None:
    """两个时间戳之间的毫秒数；任一缺失或顺序颠倒（时钟回拨）→ None。"""
    started, finished = parse_ts(started_at), parse_ts(finished_at)
    if started is None or finished is None:
        return None
    delta = int((finished - started).total_seconds() * 1000)
    return delta if delta >= 0 else None


def find_critical_path(
    durations: dict[str, int | None], dependencies: dict[str, Iterable[str]]
) -> tuple[list[str], int]:
    """返回耗时最长的依赖链（关键路径）和它的总耗时。

    记忆化 DFS：每个节点只算一次，复杂度 O(节点数 + 边数)。
    节点耗时用的是**该节点所有尝试之和** —— 重试花掉的时间确实是流水线的时间。

    环保护：正常 DAG 不会有环，但这里宁可显式报错，也不要 RecursionError
    （一个说不清原因的栈溢出比一句"DAG 里有环"难查十倍）。
    """
    best: dict[str, tuple[int, list[str]]] = {}
    visiting: set[str] = set()

    def solve(node: str) -> tuple[int, list[str]]:
        if node in best:
            return best[node]
        if node in visiting:
            raise ValueError(f"DAG 里有环，无法计算关键路径：{node}")
        visiting.add(node)
        upstream_total, upstream_path = 0, []
        for dep in dependencies.get(node, ()):
            total, path = solve(dep)
            if total > upstream_total:
                upstream_total, upstream_path = total, path
        visiting.discard(node)
        result = (upstream_total + (durations.get(node) or 0), upstream_path + [node])
        best[node] = result
        return result

    if not durations:
        return [], 0
    # sorted 而不是直接遍历 dict：并列最长时结果要**确定**，不然同一份数据两次请求
    # 可能给出不同的关键路径，测试会随机红。
    total, path = max((solve(node) for node in sorted(durations)), key=lambda item: item[0])
    return path, total


def _load_steps(db_path: str, workflow_id: str) -> list[dict[str, Any]]:
    """一次查询取回这条流水线所有任务的步骤（JOIN tasks，和 workflow_cost 同一个形状）。"""
    conn = connect(db_path, row_factory=True)
    try:
        rows = conn.execute(
            "SELECT s.task_id, s.iteration, s.type, s.tool_name, s.started_at, s.finished_at, "
            "COALESCE(s.input_tokens, 0) AS input_tokens, "
            "COALESCE(s.output_tokens, 0) AS output_tokens "
            "FROM steps s JOIN tasks t ON s.task_id = t.id "
            "WHERE t.workflow_id = ? ORDER BY s.started_at ASC",
            (workflow_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def build_trace(
    store: Any, workflow_id: str, model: str = DEFAULT_MODEL
) -> dict[str, Any] | None:
    """组装一条 workflow 的完整时间线。workflow 不存在时返回 None（端点转 404）。

    返回结构（也是 /workflows/{id}/trace 的响应体）见 README；
    关键字段：attempts（每次尝试一行，含重试）、nodes（每个节点聚合）、
    critical_path（关键路径）、slowest_node / most_tokens_node / slowest_llm_call。
    """
    workflow = store.get_workflow(workflow_id)
    if workflow is None:
        return None

    tasks = store.list_node_tasks(workflow_id)
    all_steps = _load_steps(store.db_path, workflow_id)     # 只查一次，下面两处都复用它
    steps_by_task: dict[str, list[dict[str, Any]]] = {}
    for step in all_steps:
        steps_by_task.setdefault(step["task_id"], []).append(step)

    attempts: list[dict[str, Any]] = []
    attempt_counter: dict[str, int] = {}
    for task in tasks:
        node_id = task["node_id"]
        attempt_counter[node_id] = attempt_counter.get(node_id, 0) + 1
        node_steps = steps_by_task.get(task["id"], [])
        llm_durations = [
            duration_ms(step["started_at"], step["finished_at"]) for step in node_steps
        ]
        input_tokens = sum(step["input_tokens"] for step in node_steps)
        output_tokens = sum(step["output_tokens"] for step in node_steps)
        attempts.append({
            "node_id": node_id,
            "task_id": task["id"],
            "attempt": attempt_counter[node_id],
            "status": task["status"],
            "started_at": task["started_at"],
            "finished_at": task["finished_at"],
            "duration_ms": duration_ms(task["started_at"], task["finished_at"]),
            # 纯模型延迟之和；和 duration_ms 的差 = 工具执行 + 排队 + 写库的耗时
            "llm_ms": sum(item for item in llm_durations if item is not None),
            "steps": len(node_steps),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": estimate_cost(model, input_tokens, output_tokens),
        })

    # 按节点聚合（重试会留下多行）：关键路径、最慢节点、token 最多都以节点为单位
    durations: dict[str, int] = {}
    nodes: dict[str, dict[str, Any]] = {}
    for node_id in dict.fromkeys(task["node_id"] for task in tasks):
        node_attempts = [item for item in attempts if item["node_id"] == node_id]
        total_ms = sum(item["duration_ms"] or 0 for item in node_attempts)
        durations[node_id] = total_ms
        nodes[node_id] = {
            "node_id": node_id,
            "attempts": len(node_attempts),
            "status": node_attempts[-1]["status"],       # 最新一次尝试说了算（5-G 的老规矩）
            "duration_ms": total_ms,
            "llm_ms": sum(item["llm_ms"] for item in node_attempts),
            "steps": sum(item["steps"] for item in node_attempts),
            "input_tokens": sum(item["input_tokens"] for item in node_attempts),
            "output_tokens": sum(item["output_tokens"] for item in node_attempts),
            "cost_usd": round(sum(item["cost_usd"] for item in node_attempts), 6),
        }

    # 关键路径要覆盖 DAG 里**所有**节点：没跑过的节点耗时记 0，
    # 但依赖关系还在 —— 只拿"跑过的节点"去算，可能算出一条并非真正瓶颈的链。
    dependencies = {
        node["id"]: list(node.get("depends_on") or [])
        for node in workflow["dag"].get("nodes", [])
    }
    for node_id in dependencies:
        durations.setdefault(node_id, 0)

    critical_path, critical_ms = find_critical_path(durations, dependencies)

    node_list = list(nodes.values())
    slowest_node = max(node_list, key=lambda n: n["duration_ms"], default=None)
    most_tokens_node = max(
        node_list, key=lambda n: n["input_tokens"] + n["output_tokens"], default=None
    )
    slowest_step = None
    for step in all_steps:
        step_ms = duration_ms(step["started_at"], step["finished_at"])
        if step_ms is None:
            continue
        if slowest_step is None or step_ms > slowest_step["duration_ms"]:
            slowest_step = {
                "task_id": step["task_id"], "iteration": step["iteration"],
                "type": step["type"], "tool_name": step["tool_name"],
                "duration_ms": step_ms, "input_tokens": step["input_tokens"],
                "output_tokens": step["output_tokens"],
            }

    started_values = [parse_ts(item["started_at"]) for item in attempts]
    finished_values = [parse_ts(item["finished_at"]) for item in attempts]
    started_values = [value for value in started_values if value is not None]
    finished_values = [value for value in finished_values if value is not None]
    wall_ms = None
    if started_values and finished_values:
        wall_ms = int((max(finished_values) - min(started_values)).total_seconds() * 1000)

    return {
        "workflow_id": workflow_id,
        "goal": workflow["goal"],
        "status": workflow["status"],
        "created_at": workflow["created_at"],
        "updated_at": workflow["updated_at"],
        "duration_ms": wall_ms,
        "attempts": attempts,
        "nodes": node_list,
        "critical_path": critical_path,
        "critical_path_ms": critical_ms,
        "slowest_node": slowest_node,
        "most_tokens_node": most_tokens_node,
        "slowest_llm_call": slowest_step,
        "total_cost_usd": round(sum(item["cost_usd"] for item in attempts), 6),
    }
