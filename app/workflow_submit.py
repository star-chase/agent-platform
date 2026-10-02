"""Workflow 提交服务：把一句用户目标变成一条 RUNNING 的 workflow。

和 `task_submit.py` 同一个模式：HTTP 层只做参数校验和错误码映射，
真正的逻辑放在这个**可离线单测**的服务函数里。
"""

from typing import Any

from app.planner import PlannerError, dag_to_plan


def create_workflow_from_goal(
    goal: str, planner: Any, store: Any, budget_usd: float | None = None
) -> dict[str, Any]:
    """调 Planner 规划 DAG 并落库，返回提交结果。

    budget_usd（阶段 7-A-2）：整条 workflow 的成本上限，None = 不限量。
    必须在建行的同一刻写进去：预算防线靠"每次调用前查库"生效，
    事后补写就会出现一段"以为有预算其实没有"的窗口。

    Returns:
        {"workflow_id": ..., "goal": ..., "status": "RUNNING", "budget_usd": ..., "nodes": [...]}

    Raises:
        ValueError: goal 为空，或 Planner 规划失败（PlannerError 统一转成 ValueError，
            HTTP 层再把它映射成 422）。
    """
    if not goal or not goal.strip():
        raise ValueError("goal 不能为空")
    try:
        dag = planner.plan(goal)
    except PlannerError as error:
        raise ValueError(str(error)) from error
    workflow_id = store.create_workflow(
        goal, dag_to_plan(dag), status="RUNNING", budget_usd=budget_usd
    )
    return {
        "workflow_id": workflow_id,
        "goal": goal,
        "status": "RUNNING",
        "budget_usd": budget_usd,
        "nodes": sorted(dag.nodes),
    }


