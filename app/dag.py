from typing import Any


class DAGError(ValueError):
    """DAG 结构不合法。"""


class CycleDetectedError(DAGError):
    """DAG 中检测到环。"""


class DAG:
    """保存 Agent 子任务节点及其依赖关系。"""

    def __init__(self):
        self.nodes: dict[str, Any] = {}
        # dependencies[B] = {A} 表示 B 依赖 A，也就是边 A -> B。
        self.dependencies: dict[str, set[str]] = {}

    def add_node(self, node_id: str, payload: Any) -> None:
        """添加一个子任务节点。"""
        if not node_id or not node_id.strip():
            raise DAGError("node_id 不能为空")
        if node_id in self.nodes:
            raise DAGError(f"节点已存在：{node_id}")
        self.nodes[node_id] = payload
        self.dependencies[node_id] = set()

    def add_edge(self, from_id: str, to_id: str) -> None:
        """添加 from_id -> to_id，表示 to_id 依赖 from_id。"""
        missing = [node_id for node_id in (from_id, to_id) if node_id not in self.nodes]
        if missing:
            raise DAGError(f"边引用了不存在的节点：{missing}")
        self.dependencies[to_id].add(from_id)

    def validate(self) -> None:
        """使用 Kahn 拓扑排序检测环；合法时正常返回。"""       
        indegree = {node_id: len(deps) for node_id, deps in self.dependencies.items()}
        ready = [node_id for node_id in self.nodes if indegree[node_id] == 0]
        processed = 0
        while ready:
            current = ready.pop(0)
            processed += 1
            for node_id, deps in self.dependencies.items():
                if current in deps:
                    indegree[node_id] -= 1
                    if indegree[node_id] == 0:
                        ready.append(node_id)
        if processed != len(self.nodes):
            raise CycleDetectedError("DAG 中存在循环依赖")

    def ready_nodes(self, completed: set[str]) -> list[str]:
        """返回依赖全部完成、且自身未完成的节点。"""        
        unknown = completed - self.nodes.keys()
        if unknown:
            raise DAGError(f"completed 包含不存在的节点：{sorted(unknown)}")
        return [
            node_id
            for node_id in self.nodes
            if node_id not in completed and self.dependencies[node_id] <= completed
        ]
