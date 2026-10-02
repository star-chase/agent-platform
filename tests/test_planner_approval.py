"""阶段 6-B-2：让 Planner 自己标出需要人工审批的步骤。

链路：submit_plan 的 schema（告诉 LLM 什么算"危险步骤"）
     -> plan_to_dag（字段必须原样保留，不然计划里的标记会在落库时丢掉）
     -> 调度器（见 test_approval_dispatch.py 的 6-B 门控）
"""

from app.planner import SUBMIT_PLAN_TOOL, dag_to_plan, plan_to_dag
from app.workflow_runner import DISPATCHED, WorkflowScheduler
from tests.test_workflow_runner import FakeStore


def node_schema():
    return SUBMIT_PLAN_TOOL["function"]["parameters"]["properties"]["nodes"]["items"]


def test_submit_plan_schema_documents_requires_approval():
    """schema 是给 LLM 看的契约：必须有这个字段，而且描述要说清什么算危险。"""
    properties = node_schema()["properties"]

    assert "requires_approval" in properties
    field = properties["requires_approval"]
    assert field["type"] == "boolean"
    description = field["description"]
    # 描述必须给出判断标准（不可逆/对外），否则 LLM 会乱标
    assert "不可逆" in description
    assert "对外" in description
    # 必须是可选项：省略 = 不需要审批（不能强迫每个节点都表态）
    assert "requires_approval" not in node_schema()["required"]


def test_planner_prompt_tells_llm_when_to_mark_approval():
    from app.planner import PLANNER_SYSTEM_PROMPT

    assert "requires_approval" in PLANNER_SYSTEM_PROMPT
    assert "不可逆" in PLANNER_SYSTEM_PROMPT


def test_plan_to_dag_preserves_requires_approval():
    """字段要在 plan -> DAG -> plan 的往返里活下来（不然落库就丢了）。"""
    dag = plan_to_dag({
        "nodes": [
            {"id": "collect", "description": "收集资料", "agent_type": "researcher", "depends_on": []},
            {"id": "publish", "description": "对外发布报告", "agent_type": "writer",
             "requires_approval": True, "depends_on": ["collect"]},
        ]
    })

    assert dag.nodes["publish"]["requires_approval"] is True
    assert "requires_approval" not in dag.nodes["collect"]        # 省略就是不需要

    round_tripped = plan_to_dag(dag_to_plan(dag))
    assert round_tripped.nodes["publish"]["requires_approval"] is True


def test_planner_marked_node_is_parked_by_scheduler():
    """端到端：Planner 标了 requires_approval 的节点，调度器就不许直接入队。"""
    dag = plan_to_dag({
        "nodes": [
            {"id": "publish", "description": "对外发布报告", "agent_type": "writer",
             "requires_approval": True, "depends_on": []},
        ]
    })
    store = FakeStore()
    enqueued, approvals = [], []
    scheduler = WorkflowScheduler(
        store, dag, enqueue=enqueued.append, poll_interval=0,
        request_approval=lambda workflow_id, node_id, task_id, reason: approvals.append(node_id),
    )

    assert scheduler.tick("wf-1") == DISPATCHED

    assert store.task_for("publish")["status"] == "WAITING_APPROVAL"
    assert enqueued == []
    assert approvals == ["publish"]
