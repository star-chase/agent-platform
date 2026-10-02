from dataclasses import dataclass, field
from typing import Any
import json


# 注意：WAITING_APPROVAL 只属于 task（某个节点在等人审批），workflow 此时仍是 RUNNING。
# 别把它（或 CANCELLED）加进 WORKFLOW_STATUSES：白名单每放宽一格，
# 就多一种"没人处理的合法状态"——比如编排器只轮询 RUNNING，设错状态 = workflow 永久卡死。
WORKFLOW_STATUSES = {"PLANNING", "RUNNING", "COMPLETED", "FAILED"}
# BUDGET_EXCEEDED（7-A-2）：预算花光。加个状态看着像一行改动，其实是一串白名单 —— 落一处就会出"合法状态没人处理"的鬼故事：
#   app/workflow_model.py   本白名单（建行校验）
#   app/state_machine.py    VALID_TRANSITIONS（合法转移）
#   app/cancellation.py     TERMINAL_STATUSES（终态）
#   app/workflow_store.py   get_scheduling_state（调度器怎么归类它）
#   app/workflow_runner.py  tick()（归类之后怎么处置）
#   app/main.py             is_terminal（事件流什么时候收尾）
TASK_STATUSES = {"PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "WAITING_APPROVAL", "BUDGET_EXCEEDED"}


@dataclass
class Workflow:
    id: str
    goal: str
    dag: dict[str, Any]
    status: str = "PLANNING"
    final_output: str | None = None
    created_at: str | None = None

    def validate(self) -> None:
        if not self.id or not self.id.strip():
            raise ValueError("Workflow id 不能为空")
        if not self.goal or not self.goal.strip():
            raise ValueError("Workflow goal 不能为空")
        if self.status not in WORKFLOW_STATUSES:
            raise ValueError(f"非法 Workflow 状态：{self.status}")
        if not isinstance(self.dag, dict):
            raise ValueError("dag 必须是字典")
        if not isinstance(self.dag.get("nodes"), list):
            raise ValueError("dag.nodes 必须是列表")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "dag": self.dag,
            "status": self.status,
            "final_output": self.final_output,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Workflow":
        return cls(
            id=data["id"],
            goal=data["goal"],
            dag=data["dag"],
            status=data["status"],
            final_output=data.get("final_output"),
            created_at=data.get("created_at")
        )


@dataclass
class Task:
    id: str
    workflow_id: str
    node_id: str
    question: str
    depends_on: list[str] = field(default_factory=list)
    upstream_outputs: dict[str, str] = field(default_factory=dict)
    status: str = "PENDING"
    answer: str | None = None

    def validate(self) -> None:
        if not self.id or not self.id.strip():
            raise ValueError("Task id 不能为空")
        if not self.workflow_id or not self.workflow_id.strip():
            raise ValueError("workflow_id 不能为空")
        if not self.node_id or not self.node_id.strip():
            raise ValueError("node_id 不能为空")
        if not self.question or not self.question.strip():
            raise ValueError("question 不能为空")
        if self.status not in TASK_STATUSES:
            raise ValueError(f"非法 Task 状态：{self.status}")
        if not isinstance(self.depends_on, list) or not all(
            isinstance(item, str) for item in self.depends_on
        ):
            raise ValueError("depends_on 必须是字符串列表")
        if not isinstance(self.upstream_outputs, dict):
            raise ValueError("upstream_outputs 必须是字典")
        if not all(isinstance(value, str) for value in self.upstream_outputs.values()):
            raise ValueError("upstream_outputs 的值必须是字符串")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "node_id": self.node_id,
            "question": self.question,
            "depends_on": self.depends_on,
            "upstream_outputs": self.upstream_outputs,
            "status": self.status,
            "answer": self.answer,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            id=data["id"],
            workflow_id=data["workflow_id"],
            node_id=data["node_id"],
            question=data["question"],
            depends_on=data["depends_on"],
            upstream_outputs=data["upstream_outputs"],
            status=data["status"],
            answer=data.get("answer")
        )


def dumps_model(model: Workflow | Task) -> str:
    return json.dumps(model.to_dict(), ensure_ascii=False)
