"""Workflow 提交服务测试：假 Planner + 真 SQLiteWorkflowStore（临时库）。

组合方式说明：Planner 会被换成假对象（不烧 token、不联网），
但 store 用真的临时库——因为这里要验证的核心是"规划结果有没有正确落库"，
用假 store 就测不到 JSON 序列化和状态字段是否正确。
"""

import pytest

from app.planner import PlannerError, plan_to_dag
from app.workflow_store import SQLiteWorkflowStore
from app.workflow_submit import create_workflow_from_goal


PLAN = {
    "nodes": [
        {"id": "collect", "description": "收集资料", "agent_type": "researcher", "depends_on": []},
        {"id": "write", "description": "写报告", "agent_type": "writer", "depends_on": ["collect"]},
    ]
}


class FakePlanner:
    """假 Planner：返回预置计划，或按需抛 PlannerError。"""

    def __init__(self, plan=PLAN, error=None):
        # 注意：这里**不能**写 self.plan = plan —— 实例属性会覆盖下面同名的 plan() 方法，
        # 之后 planner.plan(goal) 就会报 "'dict' object is not callable"。
        # 踩过的坑：属性名和方法名撞车，Python 里属性优先。
        self.plan_dict = plan
        self.error = error
        self.goals: list[str] = []

    def plan(self, goal):
        self.goals.append(goal)
        if self.error:
            raise PlannerError(self.error)
        return plan_to_dag(self.plan_dict)


def make_store(tmp_path):
    return SQLiteWorkflowStore(str(tmp_path / "test.db"))


def test_create_workflow_from_goal_persists_running_workflow(tmp_path):
    store = make_store(tmp_path)
    planner = FakePlanner()

    result = create_workflow_from_goal("调研并写报告", planner, store)

    assert result["status"] == "RUNNING"
    assert result["nodes"] == ["collect", "write"]
    assert planner.goals == ["调研并写报告"]

    workflow = store.get_workflow(result["workflow_id"])
    assert workflow["goal"] == "调研并写报告"
    assert workflow["status"] == "RUNNING"
    assert workflow["dag"] == PLAN  # DAG 落库后能完整 round-trip


def test_created_workflow_is_pickable_by_orchestrator(tmp_path):
    """提交后必须能被编排进程发现，否则 workflow 永远不动。"""
    store = make_store(tmp_path)
    result = create_workflow_from_goal("目标", FakePlanner(), store)
    assert store.list_workflow_ids_by_status("RUNNING") == [result["workflow_id"]]


def test_empty_goal_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="goal"):
        create_workflow_from_goal("   ", FakePlanner(), make_store(tmp_path))


def test_planner_error_becomes_value_error(tmp_path):
    planner = FakePlanner(error="DAG 中存在循环依赖")
    with pytest.raises(ValueError, match="循环"):
        create_workflow_from_goal("目标", planner, make_store(tmp_path))


# TODO X4（学生任务）：自己再加 1 个测试。
# 提示方向：① goal 为空的失败路径不应写入任何 workflow（查 list_workflow_ids_by_status）；
#          ② 规划失败时同样不应落库（避免留下没有 DAG 的僵尸 workflow）；
#          ③ dag_to_plan 是否正确保留了 agent_type/description（落库内容完整性）。
def test_planner_error_does_not_persist_workflow(tmp_path):
    """规划失败时不应落库，避免留下没有 DAG 的僵尸 workflow。"""
    store = make_store(tmp_path)
    planner = FakePlanner(error="DAG 中存在循环依赖")
    with pytest.raises(ValueError, match="循环"):
        create_workflow_from_goal("目标", planner, store)
    assert store.list_workflow_ids_by_status("RUNNING") == []
