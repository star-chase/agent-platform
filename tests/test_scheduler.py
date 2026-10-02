from app.dag import DAG
from app.scheduler import build_node_question, dispatch_ready


def diamond_dag():
    """A → (B, C) → D 的菱形依赖，用来测并行与汇聚。"""
    dag = DAG()
    dag.add_node("A", {"description": "收集原始资料"})
    dag.add_node("B", {"description": "分析市场", "depends_on": ["A"]})
    dag.add_node("C", {"description": "分析政策", "depends_on": ["A"]})
    dag.add_node("D", {"description": "写报告", "depends_on": ["B", "C"]})
    dag.add_edge("A", "B")
    dag.add_edge("A", "C")
    dag.add_edge("B", "D")
    dag.add_edge("C", "D")
    return dag


def test_dispatch_ready_starts_with_roots():
    dag = diamond_dag()
    assert dispatch_ready(dag, set(), set()) == ["A"]
    assert set(dispatch_ready(dag, {"A"}, set())) == {"B", "C"}
    assert dispatch_ready(dag, {"A", "B", "C"}, set()) == ["D"]


def test_dispatch_ready_skips_dispatched_nodes():
    """已派发但还没跑完的节点不能重复派发，否则会重复建 task、重复烧钱。"""
    dag = diamond_dag()
    assert dispatch_ready(dag, {"A"}, {"B"}) == ["C"]


def test_dispatch_ready_returns_empty_when_finished_or_blocked():
    dag = diamond_dag()
    assert dispatch_ready(dag, {"A", "B", "C", "D"}, set()) == []
    assert dispatch_ready(dag, {"A"}, {"B", "C", "D"}) == []


def test_build_node_question_contains_description_and_upstreams():
    dag = diamond_dag()
    question = build_node_question(dag, "D", {"B": "市场规模 100 亿", "C": "补贴退坡"})
    assert "写报告" in question
    assert "市场规模 100 亿" in question
    assert "补贴退坡" in question


def test_build_node_question_without_upstreams():
    dag = diamond_dag()
    question = build_node_question(dag, "A", {})
    assert "收集原始资料" in question
    assert "上游任务结果" not in question


def test_build_node_question_only_includes_direct_dependencies():
    """D 的直接上游是 B、C；A 是"上游的上游"，不应被塞进来。"""
    dag = diamond_dag()
    question = build_node_question(dag, "D", {"A": "根节点输出", "B": "b", "C": "c"})
    assert "根节点输出" not in question
    assert "b" in question and "c" in question


def test_build_node_question_truncates_long_output():
    dag = diamond_dag()
    long_output = "x" * 50
    question = build_node_question(dag, "D", {"B": long_output, "C": "短"}, max_chars=10)
    assert "…（已截断）" in question
    assert "x" * 10 in question
    assert "x" * 11 not in question


def test_build_node_question_handles_edge_cases():
    dag = diamond_dag()
    # max_chars 恰好等于长度时不应截断
    question = build_node_question(dag, "D", {"B": "1234567890", "C": "短"}, max_chars=10)
    assert "…（已截断）" not in question
    # 上游输出为空字符串时应被跳过
    question = build_node_question(dag, "D", {"B": "", "C": "短"})
    assert "【B】" not in question
    # 缺失的上游（upstream_outputs 里没有）不应让函数报错
    question = build_node_question(dag, "D", {"C": "短"})
    assert "【B】" not in question

if __name__ == "__main__":
    tests = (
        test_dispatch_ready_starts_with_roots,
        test_dispatch_ready_skips_dispatched_nodes,
        test_dispatch_ready_returns_empty_when_finished_or_blocked,
        test_build_node_question_contains_description_and_upstreams,
        test_build_node_question_without_upstreams,
        test_build_node_question_only_includes_direct_dependencies,
        test_build_node_question_truncates_long_output,
        test_build_node_question_handles_edge_cases,
    )
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
