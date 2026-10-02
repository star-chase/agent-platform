"""阶段 7-A：token 计量与成本核算。

三件事分开测：
  1) 价格换算（estimate_cost，已给实现 —— 当守卫测试用）
  2) 聚合（task_cost / workflow_cost）
  3) 记账（执行器把响应里的 usage 落到 steps 表）
"""

from app.agent_fc_persistent import AgentFCPersistentExecutor
from app.cost import estimate_cost, task_cost, workflow_cost
from app.db import connect, ensure_schema
from tests.test_dynamic_approval import APPROVAL_REQUEST
from tests.test_parallel_tool_calls import (
    FINAL_MESSAGE,
    PARALLEL_MESSAGE,
    FakeClient,
    make_executor as make_llm_executor,
)

MODEL = "deepseek-v4-flash"


def make_db(tmp_path, name="cost.db"):
    db_path = str(tmp_path / name)
    conn = connect(db_path)
    ensure_schema(conn)
    conn.close()
    return db_path


def seed_task(db_path, task_id, workflow_id=None, question="任务"):
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, question, status, max_iterations, workflow_id) "
            "VALUES (?, ?, 'PENDING', 5, ?)",
            (task_id, question, workflow_id),
        )
        conn.commit()
    finally:
        conn.close()


def seed_step(db_path, task_id, iteration, input_tokens=None, output_tokens=None):
    """直接塞一条 step（tokens 传 None 模拟老数据：列是 NULL）。"""
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO steps (id, task_id, iteration, type, observation, input_tokens, output_tokens) "
            "VALUES (?, ?, ?, 'tool_call', 'obs', ?, ?)",
            (f"{task_id}-step-{iteration}", task_id, iteration, input_tokens, output_tokens),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# A2：聚合
# ---------------------------------------------------------------------------

def test_task_cost_sums_steps_and_converts_to_usd(tmp_path):
    db_path = make_db(tmp_path)
    seed_task(db_path, "task-1")
    seed_step(db_path, "task-1", 1, input_tokens=1000, output_tokens=200)
    seed_step(db_path, "task-1", 2, input_tokens=3000, output_tokens=800)

    result = task_cost(db_path, "task-1", MODEL)

    assert result["input_tokens"] == 4000
    assert result["output_tokens"] == 1000
    # 成本必须和"单价 × token 数"对得上（用 estimate_cost 自己算一遍做交叉验证）
    assert result["cost_usd"] == estimate_cost(MODEL, 4000, 1000)


def test_task_cost_treats_null_tokens_as_zero(tmp_path):
    """老数据的 token 列是 NULL —— 聚合时不能炸，也不能把整条任务算成未知。"""
    db_path = make_db(tmp_path)
    seed_task(db_path, "task-1")
    seed_step(db_path, "task-1", 1)                     # 两个 token 列都是 NULL
    seed_step(db_path, "task-1", 2, input_tokens=1000, output_tokens=0)

    result = task_cost(db_path, "task-1", MODEL)

    assert result["input_tokens"] == 1000
    assert result["output_tokens"] == 0


def test_workflow_cost_sums_all_node_tasks(tmp_path):
    """一个 workflow 有多个节点任务 —— 成本要跨任务汇总（JOIN tasks）。"""
    db_path = make_db(tmp_path)
    seed_task(db_path, "task-a", workflow_id="wf-1")
    seed_task(db_path, "task-b", workflow_id="wf-1")
    seed_task(db_path, "task-other", workflow_id="wf-2")     # 别的 workflow，不能算进来
    seed_step(db_path, "task-a", 1, input_tokens=1000, output_tokens=100)
    seed_step(db_path, "task-b", 1, input_tokens=2000, output_tokens=200)
    seed_step(db_path, "task-other", 1, input_tokens=9999, output_tokens=9999)

    result = workflow_cost(db_path, "wf-1", MODEL)

    assert result["input_tokens"] == 3000
    assert result["output_tokens"] == 300


# ---------------------------------------------------------------------------
# A3：记账（执行器把 usage 写进 step）
# ---------------------------------------------------------------------------

def test_executor_records_usage_on_every_step(tmp_path):
    executor, _client, db_path = make_llm_executor(tmp_path, [PARALLEL_MESSAGE, FINAL_MESSAGE])

    result = executor.run("对比北京和上海的人口")

    assert result["success"] is True
    # 假响应每次都是 input=100 / output=20，两轮 -> 200 / 40
    cost = task_cost(db_path, result["task_id"], MODEL)
    assert cost["input_tokens"] == 200
    assert cost["output_tokens"] == 40


def test_approval_request_step_records_tokens(tmp_path):
    """暂停等审批的那一步也要记 token —— 它也花了一次真实的 LLM 调用。

    这条测试是被"变异测试"逼出来的：如果删掉审批步骤的 token 记账，它必须变红。
    （原来那条同名测试跑的是普通两步任务，删掉记账后依然全绿 —— 等于没测。）
    """
    db_path = make_db(tmp_path, "approval_cost.db")
    client = FakeClient([APPROVAL_REQUEST])
    executor = AgentFCPersistentExecutor(
        llm_client=client, model=MODEL, max_iterations=3, db_path=db_path,
        request_approval=lambda task_id, reason: "approval-fake",
    )

    result = executor.run("把报告发到公网")

    assert result["status"] == "WAITING_APPROVAL"      # 先确认真的走了审批路径
    conn = connect(db_path, row_factory=True)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT type, input_tokens, output_tokens FROM steps ORDER BY iteration"
        )]
    finally:
        conn.close()
    assert [r["type"] for r in rows] == ["approval_request"]
    assert rows[0]["input_tokens"] == 100              # 假响应固定 100 / 20
    assert rows[0]["output_tokens"] == 20
    


def test_get_workflow_exposes_budget_usd(tmp_path):
    """阶段 7-A 回归：迁移加了 budget_usd 列，但 get_workflow 是列名写死的 SELECT。

    这个 bug 单测全绿、真实联调才炸：端点读 workflow["budget_usd"] 直接 KeyError 500。
    教训：**加列只是第一步，所有读路径都要跟着改**（和"加状态要 grep 所有
    WHERE status IN (...)"是同一族问题）。
    """
    from app.workflow_store import SQLiteWorkflowStore

    db_path = make_db(tmp_path, "budget.db")
    store = SQLiteWorkflowStore(db_path)
    workflow_id = store.create_workflow("目标", {"nodes": []}, status="RUNNING")

    workflow = store.get_workflow(workflow_id)

    assert "budget_usd" in workflow          # 列存在且被查出来（值可以是 None）
    assert workflow["budget_usd"] is None
