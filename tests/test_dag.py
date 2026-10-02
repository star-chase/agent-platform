from app.dag import CycleDetectedError, DAG, DAGError


def build_diamond_dag() -> DAG:
    """A 完成后 B/C 可并行，B/C 都完成后 D 才能执行。"""
    dag = DAG()
    for node_id in ("A", "B", "C", "D"):
        dag.add_node(node_id, {"description": f"任务 {node_id}"})
    dag.add_edge("A", "B")
    dag.add_edge("A", "C")
    dag.add_edge("B", "D")
    dag.add_edge("C", "D")
    return dag


def test_diamond_ready_nodes():
    dag = build_diamond_dag()
    dag.validate()
    assert dag.ready_nodes(set()) == ["A"]
    assert dag.ready_nodes({"A"}) == ["B", "C"]
    assert dag.ready_nodes({"A", "B"}) == ["C"]
    assert dag.ready_nodes({"A", "B", "C"}) == ["D"]
    assert dag.ready_nodes({"A", "B", "C", "D"}) == []


def test_cycle_is_rejected():
    dag = DAG()
    for node_id in ("A", "B", "C"):
        dag.add_node(node_id, {})
    dag.add_edge("A", "B")
    dag.add_edge("B", "C")
    dag.add_edge("C", "A")
    try:
        dag.validate()
    except CycleDetectedError:
        pass
    else:
        raise AssertionError("循环依赖应该被拒绝")


def test_invalid_nodes_are_rejected():
    dag = DAG()
    dag.add_node("A", {})
    for operation in (
        lambda: dag.add_node("A", {}),
        lambda: dag.add_edge("A", "missing"),
        lambda: dag.ready_nodes({"missing"}),
    ):
        try:
            operation()
        except DAGError:
            pass
        else:
            raise AssertionError("非法节点操作应该抛出 DAGError")


def test_duplicate_edge_is_idempotent():
    dag = DAG()
    dag.add_node("A", {})
    dag.add_node("B", {})
    dag.add_edge("A", "B")
    dag.add_edge("A", "B")
    assert dag.dependencies["B"] == {"A"}


if __name__ == "__main__":
    tests = (
        test_diamond_ready_nodes,
        test_cycle_is_rejected,
        test_invalid_nodes_are_rejected,
        test_duplicate_edge_is_idempotent,
    )
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
