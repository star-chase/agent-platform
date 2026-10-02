"""阶段 7-A-2：预算上限 —— 防烧钱四层防线的最后一层。

前三层（max_iterations / 无进展检测 / 超时）管的是"**烧多少轮**"，
这一层管的直接是"**烧多少钱**"：轮数没超、进度也在推进，但单价高得离谱，照样能烧穿。

本文件的 5 条测试是这个功能的 5 个契约，按重要性排序：
  1. 闸门判断本身（含边界：正好花完算不算超）
  2. 预算从哪来（挂在 workflow 上，执行器手里只有 task_id）
  3. **闸门在 LLM 调用之前** —— 断言"一次调用都没发出去"，不是断言"任务失败了"
  4. **两个入口都装了闸门** —— 只装在 run() 上的闸门不是闸门
  5. 超支是**确定性失败**，不走节点级重试（回收阶段 6 审批的同一个道理）
"""

from app.agent_fc_persistent import AgentFCPersistentExecutor
from app.approvals import create_approval_request, decide_approval
from app.cancellation import TERMINAL_STATUSES
from app.cost import check_budget, task_budget_status
from app.db import connect, ensure_schema
from app.state_machine import transition
from app.workflow_runner import DISPATCHED, FAILED, WorkflowScheduler
from app.workflow_store import SQLiteWorkflowStore
from tests.test_dynamic_approval import APPROVAL_REQUEST
from tests.test_node_retry import diamond_dag, finish
from tests.test_parallel_tool_calls import FakeClient

MODEL = "deepseek-v4-flash"


def make_db(tmp_path, name="budget.db"):
    db_path = str(tmp_path / name)
    conn = connect(db_path)
    ensure_schema(conn)
    conn.close()
    return db_path


def seed_step(db_path, task_id, iteration, input_tokens, output_tokens):
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


def set_budget(db_path, workflow_id, budget_usd):
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE workflows SET budget_usd = ? WHERE id = ?", (budget_usd, workflow_id)
        )
        conn.commit()
    finally:
        conn.close()


def task_status(db_path, task_id):
    conn = connect(db_path)
    try:
        return conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
    finally:
        conn.close()


def make_workflow(db_path, budget_usd=None):
    """建一个带预算的 workflow，返回 (store, workflow_id)。"""
    store = SQLiteWorkflowStore(db_path)
    workflow_id = store.create_workflow("目标", {"nodes": []}, status="RUNNING",
                                        budget_usd=budget_usd)
    return store, workflow_id


# ---------------------------------------------------------------------------
# B1：闸门判断（纯函数，不碰数据库）
# ---------------------------------------------------------------------------

def test_no_budget_means_unlimited():
    """没设预算 = 不限量。单 Agent 的 /agent/run 就属于这一类，不能被拦。"""
    status = check_budget(spent_usd=99.0, budget_usd=None)

    assert status["exceeded"] is False
    assert status["remaining_usd"] is None          # None = "没有上限"，不是 0


def test_exceeded_when_spent_reaches_budget():
    """正好花完也算超支：钱已经花完了，下一笔调用必然越界。边界不能留缝。"""
    assert check_budget(spent_usd=0.01, budget_usd=0.01)["exceeded"] is True


def test_remaining_is_negative_when_over():
    """超出的部分要能看出来（负数），不能 clamp 成 0 —— 人要看的就是"超了多少"。"""
    status = check_budget(spent_usd=0.03, budget_usd=0.01)

    assert status["exceeded"] is True
    assert round(status["remaining_usd"], 6) == -0.02


# ---------------------------------------------------------------------------
# B2：预算从哪来 —— task -> workflow -> budget_usd
# ---------------------------------------------------------------------------

def test_task_budget_status_uses_workflow_budget(tmp_path):
    """执行器手里只有 task_id，预算却挂在 workflow 上：要能自己查出来。"""
    db_path = make_db(tmp_path, "lookup.db")
    store, workflow_id = make_workflow(db_path, budget_usd=0.01)
    task_id = store.create_node_task(workflow_id, "A", "问题", [])
    seed_step(db_path, task_id, 1, input_tokens=100_000, output_tokens=10_000)   # 花了约 $0.038

    status = task_budget_status(db_path, task_id, MODEL)

    assert status["workflow_id"] == workflow_id
    assert status["budget_usd"] == 0.01
    assert status["exceeded"] is True


def test_task_without_workflow_is_never_gated(tmp_path):
    """单 Agent 运行（tasks.workflow_id 是 NULL）没有预算概念，永远放行。"""
    db_path = make_db(tmp_path, "no_workflow.db")
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, question, status, max_iterations) VALUES ('t1', '问题', 'PENDING', 5)"
        )
        conn.commit()
    finally:
        conn.close()
    seed_step(db_path, "t1", 1, input_tokens=10_000_000, output_tokens=1_000_000)

    status = task_budget_status(db_path, "t1", MODEL)

    assert status["workflow_id"] is None
    assert status["exceeded"] is False


# ---------------------------------------------------------------------------
# B3：闸门必须在 LLM 调用**之前**（run 入口）
# ---------------------------------------------------------------------------

def test_run_never_calls_llm_when_over_budget(tmp_path):
    """决定性断言是 **client.completions.calls == []**，不是"任务失败了"。

    把闸门放到调用之后，任务同样会失败 —— 但钱已经花掉了。
    只有"一次调用都没发出去"才能证明闸门真的挡在消费前面（思考题 31）。
    """
    db_path = make_db(tmp_path, "gate_run.db")
    store, workflow_id = make_workflow(db_path, budget_usd=0.001)
    task_id = store.create_node_task(workflow_id, "A", "问题", [])
    seed_step(db_path, task_id, 1, input_tokens=100_000, output_tokens=10_000)

    client = FakeClient([])                  # 一条响应都没给：被调用就是 AssertionError
    executor = AgentFCPersistentExecutor(
        llm_client=client, model=MODEL, max_iterations=3, db_path=db_path,
    )

    result = executor.run("问题", task_id=task_id)

    assert client.completions.calls == []                    # ← 闸门在消费之前
    assert result["status"] == "BUDGET_EXCEEDED"
    assert result["budget_exceeded"] is True
    assert "预算" in result["answer"]                        # 理由要给人看
    assert task_status(db_path, task_id) == "BUDGET_EXCEEDED"


# ---------------------------------------------------------------------------
# B4：闸门必须装在**两个**入口上（resume 入口）
# ---------------------------------------------------------------------------

def test_resume_is_gated_too(tmp_path):
    """审批批准后走的是 resume() —— 那条路上的花费一样受预算约束。

    只装在 run() 上的闸门不是闸门：一条被暂停过的任务可以靠 resume 绕过整个预算。
    （这个坑和阶段 6 的"审批逻辑要在 run/resume 各写一遍"是同一个形状。）
    """
    db_path = make_db(tmp_path, "gate_resume.db")
    store, workflow_id = make_workflow(db_path, budget_usd=0.02)
    task_id = store.create_node_task(workflow_id, "publish", "把报告发到公网", [])

    executor = AgentFCPersistentExecutor(
        llm_client=FakeClient([APPROVAL_REQUEST]), model=MODEL,
        max_iterations=3, db_path=db_path,
        request_approval=lambda requested_task_id, reason: create_approval_request(
            db_path, requested_task_id, workflow_id, "publish", reason
        ),
    )
    paused = executor.run("把报告发到公网", task_id=task_id)
    assert paused["status"] == "WAITING_APPROVAL"            # 先确认真的停下来了
    decide_approval(db_path, paused["approval_id"], approved=True, comment="同意")

    set_budget(db_path, workflow_id, 0.000001)               # 批准后预算被收紧到必超
    client2 = FakeClient([])                                 # 同样：一次都不许被调用
    executor2 = AgentFCPersistentExecutor(
        llm_client=client2, model=MODEL, max_iterations=3, db_path=db_path,
    )

    result = executor2.resume(task_id)

    assert client2.completions.calls == []                   # ← resume 这条路上同样拦住
    assert result["status"] == "BUDGET_EXCEEDED"
    assert task_status(db_path, task_id) == "BUDGET_EXCEEDED"


# ---------------------------------------------------------------------------
# B5：超支是确定性失败 —— 不走节点级重试
# ---------------------------------------------------------------------------

def test_tick_fails_workflow_immediately_on_budget_exceeded(tmp_path):
    """重试一个"必然再次超支"的节点，只会白烧时间和日志。

    阶段 6 的审批拒绝用的是同一套推理：**确定性失败不走重试**。
    所以 BUDGET_EXCEEDED 不能混进 failed 集合，要单独告诉调度器"这条没救了"。
    """
    store = SQLiteWorkflowStore(str(tmp_path / "sched.db"))
    workflow_id = store.create_workflow("目标", {"nodes": []}, status="RUNNING")
    enqueued: list[str] = []
    scheduler = WorkflowScheduler(
        store, diamond_dag(), enqueue=enqueued.append, poll_interval=0
    )

    assert scheduler.tick(workflow_id) == DISPATCHED
    finish(store.db_path, enqueued[0], "BUDGET_EXCEEDED", "超出预算：$0.05 / $0.01")

    assert scheduler.tick(workflow_id) == FAILED
    assert store.get_workflow(workflow_id)["status"] == "FAILED"
    assert len(enqueued) == 1                                # ← 没有第二次派发


def test_latest_attempt_wins_over_earlier_budget_exceeded(tmp_path):
    """超支的那次尝试被后来的成功覆盖时，不能把已经跑完的 workflow 判死。

    "最新一次尝试说了算"（5-G 的立身之本）对 **fatal** 也必须成立：
    每个分支都要把另外两个集合清掉。漏掉 fatal.discard 就会重现 5-G 那个 bug ——
    同一个节点同时出现在两个集合里，结果取决于代码里 if 的先后顺序。
    """
    store = SQLiteWorkflowStore(str(tmp_path / "latest.db"))
    workflow_id = store.create_workflow("目标", {"nodes": []}, status="RUNNING")

    first = store.create_node_task(workflow_id, "A", "第一次", [])
    finish(store.db_path, first, "BUDGET_EXCEEDED", "超支")
    second = store.create_node_task(workflow_id, "A", "第二次", [])
    finish(store.db_path, second, "COMPLETED", "这次成功了")

    state = store.get_scheduling_state(workflow_id)

    assert state["fatal"] == set()
    assert "A" in state["completed"]
    assert "A" not in state["failed"]


# ---------------------------------------------------------------------------
# B6：加状态 = 一串白名单都要跟着改（"ripple 清单"的可执行版本）
# ---------------------------------------------------------------------------

def test_budget_exceeded_is_a_legal_terminal_task_status(tmp_path):
    """新状态要在三个白名单里同时存在，否则总有一处把合法状态当成"还在跑"：
        workflow_model.TASK_STATUSES   —— 建行时校验
        state_machine.VALID_TRANSITIONS —— 合法转移
        cancellation.TERMINAL_STATUSES  —— 终态判断（事件流什么时候收尾）
    """
    store = SQLiteWorkflowStore(str(tmp_path / "status.db"))
    workflow_id = store.create_workflow("目标", {"nodes": []}, status="RUNNING")
    task_id = store.create_node_task(workflow_id, "A", "问题", [],
                                     status="BUDGET_EXCEEDED")

    assert task_id
    assert transition({"status": "RUNNING"}, "BUDGET_EXCEEDED")["status"] == "BUDGET_EXCEEDED"
    assert "BUDGET_EXCEEDED" in TERMINAL_STATUSES
