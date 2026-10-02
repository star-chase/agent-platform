"""阶段 7-C：简易追踪。

分两层测，因为"算得对"和"数据齐"是两个独立的失败点：

  ① **纯函数**（parse/duration/关键路径）—— 不碰数据库，可以构造极端输入
     （缺时间戳、时钟回拨、并列最长、有环）。
  ② **组装 + 数据基座** —— 从临时库里读真实的行，验证 trace 的三个结论
     （哪一步最慢 / 哪一步 token 最多 / 关键路径）真的对得上。

第 ② 层里最重要的两条是 `test_steps_record_real_llm_latency` 和
`test_task_row_gets_started_and_finished_at`：它们保护的**不是 trace 的算法，
而是喂给算法的数据**。阶段 7-C 之前，steps 的 started_at 和 finished_at
都是在 INSERT 那一刻取的，两者几乎相同 —— 那一列等于没在用，
而 trace 再聪明也算不出"耗时 0"背后的真相。
"""

import time

import pytest

from app.agent_fc_persistent import AgentFCPersistentExecutor
from app.db import connect, ensure_schema
from app.trace import build_trace, duration_ms, find_critical_path
from app.workflow_store import SQLiteWorkflowStore
from tests.test_parallel_tool_calls import FINAL_MESSAGE, FakeClient
from tests.test_worker_runtime_profile import make_runtime, seed_task, seed_workflow


def make_db(tmp_path, name="test.db"):
    """本文件自己的建库助手（另外两个测试文件各有一个同名不同签名的 make_db，
    交叉导入过一次就撞了 —— 名字一样、行为不一样，是最难发现的测试事故）。"""
    db_path = str(tmp_path / name)
    conn = connect(db_path)
    ensure_schema(conn)
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# C1：纯函数
# ---------------------------------------------------------------------------

def test_parse_and_duration():
    assert duration_ms("2026-10-01 08:00:00.000", "2026-10-01 08:00:01.250") == 1250
    # 秒级时间戳（老数据）也能解析 —— 两种格式同族
    assert duration_ms("2026-10-01 08:00:00", "2026-10-01 08:00:02") == 2000


def test_duration_is_none_when_data_is_missing_or_backwards():
    """缺失/异常必须返回 None，**不能返回 0**。

    "不知道多久"和"瞬间完成"是两件事：混成 0 会让"最慢节点"的排名悄悄错掉
    （一个没跑完的节点看起来比谁都快）。
    """
    assert duration_ms(None, "2026-10-01 08:00:01") is None
    assert duration_ms("2026-10-01 08:00:01", None) is None
    assert duration_ms("不是时间", "2026-10-01 08:00:01") is None
    assert duration_ms("2026-10-01 08:00:02", "2026-10-01 08:00:01") is None  # 时钟回拨


def test_critical_path_picks_the_longest_dependency_chain():
    """A→(B,C)→D：B 很慢、C 很快 → 关键路径必须走 B 那条。

    A=1s, B=5s, C=0.2s, D=3s
      A→B→D = 9.0s   ← 关键路径
      A→C→D = 4.2s
    """
    durations = {"A": 1000, "B": 5000, "C": 200, "D": 3000}
    dependencies = {"A": [], "B": ["A"], "C": ["A"], "D": ["B", "C"]}

    path, total = find_critical_path(durations, dependencies)

    assert path == ["A", "B", "D"]
    assert total == 9000


def test_critical_path_result_is_deterministic_on_ties():
    """并列最长时结果必须确定 —— 否则同一份数据两次请求给出不同答案，测试随机红。"""
    durations = {"A": 1000, "B": 1000}
    dependencies = {"A": [], "B": []}

    assert find_critical_path(durations, dependencies) == (
        find_critical_path(durations, dependencies)[0], 1000
    )
    assert find_critical_path(durations, dependencies)[0] == ["A"]


def test_critical_path_treats_unknown_duration_as_zero():
    """没跑过的节点耗时按 0 算，但依赖关系还在（否则会算出并非真正瓶颈的链）。"""
    durations = {"A": 100, "B": None, "C": 50}
    dependencies = {"A": [], "B": ["A"], "C": ["B"]}

    path, total = find_critical_path(durations, dependencies)

    assert path == ["A", "B", "C"]
    assert total == 150


def test_critical_path_refuses_a_cycle():
    """有环时显式报错。RecursionError 说不清原因，比一句"DAG 里有环"难查十倍。"""
    with pytest.raises(ValueError, match="环"):
        find_critical_path({"A": 1, "B": 1}, {"A": ["B"], "B": ["A"]})


def test_empty_dag_returns_empty_path():
    assert find_critical_path({}, {}) == ([], 0)


# ---------------------------------------------------------------------------
# C2：组装 —— 从真实库里读出来
# ---------------------------------------------------------------------------

DIAMOND_DAG = {
    "nodes": [
        {"id": "A", "description": "收集", "depends_on": []},
        {"id": "B", "description": "慢分支", "depends_on": ["A"]},
        {"id": "C", "description": "快分支", "depends_on": ["A"]},
        {"id": "D", "description": "汇总", "depends_on": ["B", "C"]},
    ]
}

# 用固定时间戳构造一条"时间分布和 token 分布故意相反"的流水线：
#   A 2s / B 6s（最慢）/ C 2s / D 1s
#   token 最多的是 C（钱的分布在 C，时间的分布在 B）
TIMELINE = {
    "A": ("2026-10-01 10:00:00.000", "2026-10-01 10:00:02.000"),
    "B": ("2026-10-01 10:00:02.000", "2026-10-01 10:00:08.000"),
    "C": ("2026-10-01 10:00:02.000", "2026-10-01 10:00:04.000"),
    "D": ("2026-10-01 10:00:08.000", "2026-10-01 10:00:09.000"),
}
TOKENS = {"A": (100, 10), "B": (200, 20), "C": (5000, 500), "D": (300, 30)}


def seed_workflow_row(db_path, workflow_id="wf-1", dag=DIAMOND_DAG, status="COMPLETED"):
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO workflows (id, goal, status, dag, created_at) "
            "VALUES (?, '目标', ?, ?, '2026-10-01 09:59:59.000')",
            (workflow_id, status, __import__("json").dumps(dag, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


def seed_node(db_path, workflow_id, node_id, task_id, status="COMPLETED"):
    started, finished = TIMELINE.get(node_id, (None, None))
    input_tokens, output_tokens = TOKENS.get(node_id, (0, 0))
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, question, status, max_iterations, workflow_id, node_id, "
            "started_at, finished_at) VALUES (?, ?, ?, 5, ?, ?, ?, ?)",
            (task_id, f"问题 {node_id}", status, workflow_id, node_id, started, finished),
        )
        # 一条 step 代表那次 LLM 调用：起止时间差 = 模型延迟
        conn.execute(
            "INSERT INTO steps (id, task_id, iteration, type, observation, started_at, "
            "finished_at, input_tokens, output_tokens) "
            "VALUES (?, ?, 1, 'final_answer', '答案', ?, ?, ?, ?)",
            (f"{task_id}-s1", task_id, started, finished, input_tokens, output_tokens),
        )
        conn.commit()
    finally:
        conn.close()


def build_seeded_trace(tmp_path):
    db_path = make_db(tmp_path, "trace.db")
    store = SQLiteWorkflowStore(db_path)
    seed_workflow_row(db_path)
    for node_id in ("A", "B", "C", "D"):
        seed_node(db_path, "wf-1", node_id, f"task-{node_id}")
    return store, db_path, build_trace(store, "wf-1")


def test_trace_answers_which_step_is_slowest(tmp_path):
    _store, _db, trace = build_seeded_trace(tmp_path)

    assert trace["slowest_node"]["node_id"] == "B"
    assert trace["slowest_node"]["duration_ms"] == 6000
    # 总墙钟 = 最早开始到最晚结束（10:00:00 → 10:00:09）
    assert trace["duration_ms"] == 9000


def test_trace_answers_which_step_used_most_tokens(tmp_path):
    """钱的分布和时间分布**不在同一个节点**上 —— 这正是要分开报两个结论的原因。"""
    _store, _db, trace = build_seeded_trace(tmp_path)

    assert trace["most_tokens_node"]["node_id"] == "C"
    assert trace["most_tokens_node"]["input_tokens"] == 5000
    assert trace["most_tokens_node"]["node_id"] != trace["slowest_node"]["node_id"]


def test_trace_reports_the_critical_path(tmp_path):
    _store, _db, trace = build_seeded_trace(tmp_path)

    assert trace["critical_path"] == ["A", "B", "D"]
    assert trace["critical_path_ms"] == 9000


def test_trace_sums_every_attempt_of_a_retried_node(tmp_path):
    """重试会留下多条 task 行 —— trace 必须把**每次尝试**都列出来。

    重试花掉的时间就是流水线的时间；只报最后一次成功的话，
    "这个节点为什么这么慢"就永远看不出来。
    """
    db_path = make_db(tmp_path, "retry.db")
    store = SQLiteWorkflowStore(db_path)
    seed_workflow_row(db_path)
    seed_node(db_path, "wf-1", "A", "task-A-1", status="FAILED")
    # 第二次尝试：换一条 task 行（节点级重试就是这么干的）
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, question, status, max_iterations, workflow_id, node_id, "
            "started_at, finished_at) VALUES ('task-A-2', '再来一次', 'COMPLETED', 5, 'wf-1', 'A', "
            "'2026-10-01 10:00:10.000', '2026-10-01 10:00:13.000')"
        )
        conn.commit()
    finally:
        conn.close()

    trace = build_trace(store, "wf-1")

    node_a = next(item for item in trace["attempts"] if item["node_id"] == "A")
    attempts_of_a = [item for item in trace["attempts"] if item["node_id"] == "A"]
    assert len(attempts_of_a) == 2
    assert [item["status"] for item in attempts_of_a] == ["FAILED", "COMPLETED"]
    assert node_a["attempt"] == 1
    # 节点总耗时 = 两次尝试之和（2000 + 3000），不是最后一次的 3000
    aggregated = next(item for item in trace["nodes"] if item["node_id"] == "A")
    assert aggregated["duration_ms"] == 5000
    assert aggregated["attempts"] == 2


def test_trace_returns_none_for_unknown_workflow(tmp_path):
    """端点靠这个 None 转 404。"""
    store = SQLiteWorkflowStore(make_db(tmp_path, "empty.db"))

    assert build_trace(store, "不存在的-workflow") is None


def test_trace_marks_unknown_duration_as_none_not_zero(tmp_path):
    """没跑完的节点（比如超预算被拦下）started_at/finished_at 是 NULL。"""
    db_path = make_db(tmp_path, "blocked.db")
    store = SQLiteWorkflowStore(db_path)
    seed_workflow_row(db_path, status="FAILED")
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, question, status, max_iterations, workflow_id, node_id) "
            "VALUES ('task-A', '问题', 'BUDGET_EXCEEDED', 5, 'wf-1', 'A')"
        )
        conn.commit()
    finally:
        conn.close()

    trace = build_trace(store, "wf-1")

    attempt = next(item for item in trace["attempts"] if item["node_id"] == "A")
    assert attempt["status"] == "BUDGET_EXCEEDED"
    assert attempt["duration_ms"] is None          # 不是 0
    assert trace["nodes"][0]["duration_ms"] == 0   # 聚合时缺失按 0 计入关键路径


# ---------------------------------------------------------------------------
# C3：数据基座 —— 喂给算法的时间戳必须是真的
# ---------------------------------------------------------------------------

def test_steps_record_real_llm_latency(tmp_path):
    """step 的 started_at 必须是"LLM 调用开始"的时刻，不是写库的时刻。

    改之前：started_at 和 finished_at 都在 INSERT 那一刻取 → 差恒为 0，
    这两列等于没在用，trace 里每次调用都"零延迟"。
    """
    db_path = make_db(tmp_path, "latency.db")
    client = FakeClient([FINAL_MESSAGE])
    original_create = client.completions.create

    def slow_create(**kwargs):
        time.sleep(0.06)                      # 模拟一次 60ms 的模型调用
        return original_create(**kwargs)

    client.completions.create = slow_create
    executor = AgentFCPersistentExecutor(
        llm_client=client, model="fake-model", max_iterations=3, db_path=db_path
    )

    result = executor.run("问题")

    conn = connect(db_path, row_factory=True)
    try:
        row = dict(conn.execute(
            "SELECT started_at, finished_at FROM steps WHERE task_id = ?",
            (result["task_id"],),
        ).fetchone())
    finally:
        conn.close()
    elapsed = duration_ms(row["started_at"], row["finished_at"])
    assert elapsed is not None and elapsed >= 50, row


def test_task_row_gets_started_and_finished_at(tmp_path):
    """task 行的起止由 worker_runtime 记：它在 finally 里写结束时间，失败路径也有。"""
    db_path = make_db(tmp_path, "task_times.db")
    seed_workflow(db_path)
    seed_task(db_path, node_id="write")

    client = FakeClient([FINAL_MESSAGE])
    original_create = client.completions.create

    def slow_create(**kwargs):
        time.sleep(0.06)
        return original_create(**kwargs)

    client.completions.create = slow_create
    runtime = make_runtime(db_path, client=client)
    runtime.run_task(runtime.load_task("task-1"))

    conn = connect(db_path, row_factory=True)
    try:
        row = dict(conn.execute(
            "SELECT started_at, finished_at FROM tasks WHERE id = 'task-1'"
        ).fetchone())
    finally:
        conn.close()

    assert row["started_at"] and row["finished_at"]
    assert duration_ms(row["started_at"], row["finished_at"]) >= 50
