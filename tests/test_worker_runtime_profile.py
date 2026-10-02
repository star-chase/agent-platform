"""阶段 5-E-2：worker 按子任务节点的角色装配执行器。

链路：tasks.node_id → workflows.dag 里对应节点的 agent_type → get_profile()
     → 执行器的 system_prompt / tool_names

这里用真实 SQLiteWorkflowStore 的临时库（不是假对象），因为要验证的正是
"库里存的 DAG JSON 和 runtime 的假设对不对得上"。
"""

import json

from app.agent_profiles import AGENT_PROFILES
from app.db import connect
from app.tools import TOOLS
from app.worker_runtime import PersistentTaskRuntime
from tests.test_parallel_tool_calls import FINAL_MESSAGE, FakeClient, make_db


DAG = {
    "nodes": [
        {"id": "collect", "description": "收集资料", "agent_type": "researcher", "depends_on": []},
        {"id": "write", "description": "写报告", "agent_type": "writer", "depends_on": ["collect"]},
    ]
}


class FakeRedis:
    """发表进度事件的假客户端：只记录，不真连 Redis。"""

    def __init__(self):
        self.published = []

    def publish(self, channel, payload):
        self.published.append((channel, payload))


def seed_workflow(db_path, workflow_id="wf-1", dag=DAG):
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO workflows (id, goal, status, dag) VALUES (?, '目标', 'RUNNING', ?)",
            (workflow_id, json.dumps(dag, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


def seed_task(db_path, task_id="task-1", node_id="write", workflow_id="wf-1", question="写报告"):
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, question, status, max_iterations, workflow_id, node_id) "
            "VALUES (?, ?, 'PENDING', 5, ?, ?)",
            (task_id, question, workflow_id, node_id),
        )
        conn.commit()
    finally:
        conn.close()


def seed_legacy_task(db_path, task_id="legacy"):
    """阶段 1-4 的老任务：没有 workflow_id / node_id。"""
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, question, status, max_iterations) "
            "VALUES (?, '随便问点什么', 'PENDING', 5)",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()


def make_runtime(db_path, client=None):
    return PersistentTaskRuntime(
        db_path=db_path,
        redis_client=FakeRedis(),
        llm_client=client,
    )


# ---------------------------------------------------------------------------
# W1：load_task 必须带回 workflow_id / node_id
# ---------------------------------------------------------------------------

def test_load_task_returns_workflow_and_node_id(tmp_path):
    db_path = make_db(tmp_path)
    seed_workflow(db_path)
    seed_task(db_path, node_id="write")

    task = make_runtime(db_path).load_task("task-1")

    assert task["workflow_id"] == "wf-1"
    assert task["node_id"] == "write"


# ---------------------------------------------------------------------------
# W2：按节点 agent_type 解析角色档案
# ---------------------------------------------------------------------------

def test_resolve_profile_returns_node_profile(tmp_path):
    db_path = make_db(tmp_path)
    seed_workflow(db_path)
    seed_task(db_path, node_id="write")
    runtime = make_runtime(db_path)

    profile = runtime._resolve_profile(runtime.load_task("task-1"))

    assert profile is not None
    assert profile.name == "writer"
    assert profile.tools == ()


def test_resolve_profile_handles_researcher_node(tmp_path):
    db_path = make_db(tmp_path)
    seed_workflow(db_path)
    seed_task(db_path, task_id="task-r", node_id="collect", question="收集资料")
    runtime = make_runtime(db_path)

    profile = runtime._resolve_profile(runtime.load_task("task-r"))

    assert profile.name == "researcher"
    assert profile.tools == ("search",)


def test_resolve_profile_returns_none_for_legacy_task(tmp_path):
    """老任务没有 workflow_id / node_id，必须安静地退回默认行为，不能报错。"""
    db_path = make_db(tmp_path)
    seed_legacy_task(db_path)
    runtime = make_runtime(db_path)

    assert runtime._resolve_profile(runtime.load_task("legacy")) is None


def test_resolve_profile_returns_none_for_unknown_node(tmp_path):
    db_path = make_db(tmp_path)
    seed_workflow(db_path)
    seed_task(db_path, node_id="不在 DAG 里的节点")
    runtime = make_runtime(db_path)

    assert runtime._resolve_profile(runtime.load_task("task-1")) is None


# ---------------------------------------------------------------------------
# W3：run_task 把档案装进执行器
# ---------------------------------------------------------------------------

def test_run_task_uses_profile_prompt_and_tools(tmp_path):
    db_path = make_db(tmp_path)
    seed_workflow(db_path)
    seed_task(db_path, node_id="write")

    client = FakeClient([FINAL_MESSAGE])
    runtime = make_runtime(db_path, client=client)
    runtime.run_task(runtime.load_task("task-1"))

    call = client.completions.calls[0]
    assert call["messages"][0]["content"].startswith(AGENT_PROFILES["writer"].system_prompt)
    # writer 没有业务工具，但控制面工具（请求人工审批）始终可用（阶段 6-D）
    assert [t["function"]["name"] for t in call["tools"]] == ["request_human_approval"]


def test_run_task_without_profile_keeps_defaults(tmp_path):
    """没有角色信息时保持旧行为：默认提示词 + 全部工具。"""
    db_path = make_db(tmp_path)
    seed_legacy_task(db_path)

    client = FakeClient([FINAL_MESSAGE])
    runtime = make_runtime(db_path, client=client)
    runtime.run_task(runtime.load_task("legacy"))

    call = client.completions.calls[0]
    assert "你是一个智能助手" in call["messages"][0]["content"]
    assert {tool["function"]["name"] for tool in call["tools"]} == set(TOOLS) | {"request_human_approval"}


def test_run_task_with_analyst_profile(tmp_path):
    db_path = make_db(tmp_path)
    # 创建一个包含 analyst 节点的 DAG
    dag_with_analyst = {
        "nodes": [
            {"id": "analyze", "description": "分析数据", "agent_type": "analyst", "depends_on": []},
        ]
    }
    seed_workflow(db_path, workflow_id="wf-2", dag=dag_with_analyst)
    seed_task(db_path, task_id="task-2", node_id="analyze", workflow_id="wf-2", question="分析数据")

    client = FakeClient([FINAL_MESSAGE])
    runtime = make_runtime(db_path, client=client)
    runtime.run_task(runtime.load_task("task-2"))

    call = client.completions.calls[0]
    assert call["messages"][0]["content"].startswith(AGENT_PROFILES["analyst"].system_prompt)
    assert {tool["function"]["name"] for tool in call["tools"]} == {
        "calculator", "search", "request_human_approval",
    }


# ---------------------------------------------------------------------------
# W4：run_task 的日志上下文接线（阶段 7-B）
# ---------------------------------------------------------------------------

def test_run_task_logs_carry_the_task_context(tmp_path):
    """run_task 里绑的上下文，必须真的出现在**别人**打出来的日志里。

    这条测试保护的是"绑一次，处处都有"这个核心承诺：
      - task_started / task_completed 由 run_task 自己打（上下文肯定在）；
      - **step_saved 由执行器深处打** —— 它一行的参数里没有 task_id，
        却必须带上 task_id。这才证明上下文真的穿透了十几层。

    为什么必须有这条测试：忘了包 `with log_context(...)` 是**静默失败** ——
    日志照常输出、测试照常全绿，只是每条都没有 task_id，
    于是 `grep <task_id> app.log` 永远查不到东西（验收标准当场失效）。
    """
    import io
    import json

    from app.logging_setup import setup_logging

    db_path = make_db(tmp_path)
    seed_workflow(db_path)
    seed_task(db_path, node_id="write")

    buffer = io.StringIO()
    setup_logging(level="INFO", stream=buffer)

    runtime = make_runtime(db_path, client=FakeClient([FINAL_MESSAGE]))
    runtime.run_task(runtime.load_task("task-1"))

    events = [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]
    by_event = {event["event"]: event for event in events}

    started = by_event["task_started"]
    assert (started["task_id"], started["workflow_id"], started["node_id"]) == (
        "task-1", "wf-1", "write",
    )
    assert by_event["task_completed"]["status"] == "COMPLETED"
    # 执行器深处打的日志，同样带着入口绑的上下文
    assert by_event["step_saved"]["task_id"] == "task-1"
