"""Workflow 级进度事件（阶段 5-H）。

两层进度怎么分工：
    task 级（阶段 4 已有） —— 一个 Agent 内部每一步：/tasks/{id}/events
    workflow 级（本文件）  —— 整个目标的状态 + 每个节点跑到哪了：/workflows/{id}/events

前端一旦订阅 workflow 频道，第一件事必须收到**全量快照**：
Redis Pub/Sub 不保存历史，新订阅者只靠实时事件会看到一片空白。
快照从数据库折叠出来（**数据库才是事实来源**，事件只是"变更通知"）。
"""

from typing import Any

TERMINAL_WORKFLOW_STATUSES = frozenset({"COMPLETED", "FAILED"})


def is_terminal_workflow(status: str | None) -> bool:
    """workflow 是否已进入终态（终态之后不会再有新事件）。"""
    return status in TERMINAL_WORKFLOW_STATUSES


def build_snapshot(workflow: dict[str, Any], tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 workflow + 子任务行折叠成发给新订阅者的快照事件列表。

    期望返回（第一条是整体状态，后面每个节点一条）：
        [
          {"type": "workflow_state", "workflow_id": "wf-1", "goal": "调研", "status": "RUNNING"},
          {"type": "node_state", "node_id": "A", "status": "COMPLETED", "attempt": 2,
           "answer": "..."},
          ...
        ]

    折叠规则（和 5-G 在 SQL 里做的是同一件事，只是换到了内存里）：
      1. 第一条永远是 workflow_state —— 前端先拿到整体状态和 goal。
      2. 每个节点只发**一条** node_state：一个节点可能有多条 task 行（重试），
         按创建先后遍历、**后来者覆盖前者**，留下的就是最新一次尝试。
      3. attempt = 该节点一共几条 task 行（第几次尝试，首次为 1）。
      4. 按 node_id 排序输出 —— 顺序稳定，前端好做 diff、测试也好写。
      5. node_id 为 None 的行（阶段 4 那些没有 DAG 节点的老任务）跳过。
    """
    workflow_id = workflow["id"]
    goal = workflow.get("goal")
    status = workflow.get("status")
    snapshot = [{"type": "workflow_state", "workflow_id": workflow_id, "goal": goal, "status": status}]
    node_states = {}
    for task in tasks:
        node_id = task.get("node_id")
        if node_id is None:
            continue
        node_states[node_id] = {
            "type": "node_state",
            "node_id": node_id,
            "status": task.get("status"),
            "attempt": node_states.get(node_id, {}).get("attempt", 0) + 1,
            "answer": task.get("answer"),
        }
    snapshot.extend(sorted(node_states.values(), key=lambda x: x["node_id"]))
    return snapshot
