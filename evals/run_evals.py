#!/usr/bin/env python3
"""Agent Platform 场景评测（evals）。

单元测试测边界；evals 测**场景**：用真实的 SQLiteWorkflowStore、WorkflowScheduler、
cost / approvals / trace / state_machine，只在两处用脚本替身 ——
「模型输出」和「worker 执行结果」，于是整套评测离线可跑、结果可复现。

产出：每个场景 期望 vs 实际 的评分卡 + 分类通过率 + 关键指标（尝试次数、派发宽度、终态）。

用法：
    python -m evals.run_evals                        # 跑全部场景，打印摘要
    python -m evals.run_evals --report evals/report.md
    python -m evals.run_evals --check                # 有失败场景时退出码为 1（CI 用）
    python -m evals.run_evals --category governance  # 只跑某一类
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.approvals import create_approval_request, decide_approval  # noqa: E402
from app.cancellation import cancel_task  # noqa: E402
from app.cost import check_budget, estimate_cost, workflow_cost  # noqa: E402
from app.dag import DAG  # noqa: E402
from app.db import connect, ensure_schema  # noqa: E402
from app.planner import PlannerError, plan_to_dag  # noqa: E402
from app.progress_guard import NoProgressDetector  # noqa: E402
from app.state_machine import InvalidTransitionError, transition  # noqa: E402
from app.trace import build_trace  # noqa: E402
from app.workflow_runner import COMPLETED, FAILED, WorkflowScheduler, sink_nodes  # noqa: E402
from app.workflow_store import SQLiteWorkflowStore  # noqa: E402

SCENARIOS = Path(__file__).with_name("scenarios.jsonl")
DEFAULT_MODEL = "deepseek-v4-flash"


@dataclass
class Result:
    scenario_id: str
    category: str
    name: str
    passed: bool
    expected: dict
    observed: dict
    note: str = ""

    def as_row(self) -> dict:
        return {
            "id": self.scenario_id,
            "category": self.category,
            "name": self.name,
            "passed": self.passed,
            "expected": self.expected,
            "observed": self.observed,
            "note": self.note,
        }


def load_scenarios(path: Path, category: str | None = None) -> list[dict]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        scenario = json.loads(line)
        if category and scenario["category"] != category:
            continue
        items.append(scenario)
    return items


def build_dag(spec: dict) -> DAG:
    return plan_to_dag(spec, max_nodes=spec.get("max_nodes", 50))


def db_path_for(tmp: Path, name: str) -> str:
    path = str(tmp / f"{name}.db")
    conn = connect(path)
    ensure_schema(conn)
    conn.close()
    return path


def finish(db_path: str, task_id: str, status: str, answer: str | None = None) -> None:
    """模拟 worker 跑完一次尝试：把 task 行写成 COMPLETED / FAILED / BUDGET_EXCEEDED。"""
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET status = ?, answer = ? WHERE id = ?",
            (status, answer, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def seed_step(
    db_path: str,
    task_id: str,
    iteration: int,
    input_tokens: int,
    output_tokens: int,
) -> None:
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


def set_budget(db_path: str, workflow_id: str, budget_usd: float) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE workflows SET budget_usd = ? WHERE id = ?", (budget_usd, workflow_id)
        )
        conn.commit()
    finally:
        conn.close()


def task_statuses(store: SQLiteWorkflowStore, workflow_id: str) -> dict[str, str]:
    """每个节点"最新一次尝试"的状态（和调度器读的语义保持一致）。"""
    latest: dict[str, str] = {}
    for task in store.list_node_tasks(workflow_id):
        if task["node_id"]:
            latest[task["node_id"]] = task["status"]
    return latest


def run_plan(scenario: dict, tmp: Path) -> Result:
    expect = scenario["expect"]
    observed: dict = {}
    note = ""
    try:
        dag = plan_to_dag(scenario["plan"], max_nodes=scenario["plan"].get("max_nodes", 10))
    except (PlannerError, ValueError) as exc:
        observed = {"ok": False, "error": type(exc).__name__}
        passed = expect.get("ok") is False
        return Result(scenario["id"], scenario["category"], scenario["name"], passed, expect, observed, note)

    observed = {
        "ok": True,
        "nodes": sorted(dag.nodes),
        "edges": sorted([dep, node] for node, deps in dag.dependencies.items() for dep in deps),
        "sinks": sorted(sink_nodes(dag)),
        "ready_initial": sorted(dag.ready_nodes(set())),
    }
    if "ready_after" in expect:
        after = set(expect["ready_after"]["completed"])
        observed["ready_after"] = sorted(dag.ready_nodes(after))
    passed = all(
        observed.get(key) == (value["ready"] if key == "ready_after" else value)
        for key, value in expect.items()
    )
    return Result(scenario["id"], scenario["category"], scenario["name"], passed, expect, observed, note)


def simulate_workflow(scenario: dict, tmp: Path) -> Result:
    """脚本化跑一条 workflow：真实 store + 真实调度器，worker 结果与审批决定按脚本给。"""
    dag = build_dag(scenario["dag"])
    db_path = db_path_for(tmp, scenario["id"])
    store = SQLiteWorkflowStore(db_path)
    workflow_id = store.create_workflow(scenario["name"], scenario["dag"], status="RUNNING")
    if scenario.get("budget_usd") is not None:
        set_budget(db_path, workflow_id, scenario["budget_usd"])

    script: dict[str, list[str]] = scenario.get("script", {})
    decisions: dict[str, str] = scenario.get("approvals", {})
    attempts: dict[str, int] = {node_id: 0 for node_id in dag.nodes}
    pending: list[str] = []
    approvals_by_node: dict[str, tuple[str, str]] = {}
    dispatch_widths: list[int] = []
    approval_waits: list[int] = []

    def enqueue(task_id: str) -> None:
        pending.append(task_id)

    def request_approval(wf: str, node_id: str, task_id: str, reason: str | None) -> None:
        approval_id = create_approval_request(db_path, task_id, wf, node_id, reason)
        approvals_by_node[node_id] = (approval_id, task_id)

    scheduler = WorkflowScheduler(
        store,
        dag,
        enqueue,
        poll_interval=0,
        max_ticks=scenario.get("max_ticks", 12),
        max_node_attempts=scenario.get("max_node_attempts", 3),
        request_approval=request_approval,
    )

    terminal = "NONE"
    for _ in range(scenario.get("max_ticks", 12)):
        terminal = scheduler.tick(workflow_id)
        dispatch_widths.append(len(scheduler.last_dispatched))
        approval_waits.append(len(scheduler.last_waiting_approval))
        if terminal in (COMPLETED, FAILED):
            break
        for node_id in scheduler.last_waiting_approval:
            approval_id, task_id = approvals_by_node[node_id]
            decision = decisions.get(node_id, "approve")
            outcome = decide_approval(db_path, approval_id, approved=decision == "approve")
            if outcome["should_enqueue"]:
                pending.append(outcome["task_id"])
        still_running: list[str] = []
        for task_id in pending:
            node_id = next(
                (t["node_id"] for t in store.list_node_tasks(workflow_id) if t["id"] == task_id),
                None,
            )
            if node_id is None:
                continue
            index = attempts[node_id]
            statuses = script.get(node_id, [])
            attempts[node_id] = index + 1
            if index < len(statuses):
                finish(db_path, task_id, statuses[index], answer=f"{node_id}-out-{index}")
            else:
                still_running.append(task_id)  # 脚本没给结果：这个节点一直挂着
        pending = still_running

    state = store.get_scheduling_state(workflow_id)
    workflow = store.get_workflow(workflow_id) or {}
    observed = {
        "terminal": terminal,
        "workflow_status": workflow.get("status"),
        "attempts": state["attempts"],
        "max_dispatch_width": max(dispatch_widths) if dispatch_widths else 0,
        "final_output": workflow.get("final_output"),
        "task_status": task_statuses(store, workflow_id),
    }
    if "final_output_contains" in scenario["expect"]:
        observed["final_output_contains"] = scenario["expect"]["final_output_contains"] in (
            workflow.get("final_output") or ""
        )
    if "waited_for_approval" in scenario["expect"]:
        observed["waited_for_approval"] = max(approval_waits) >= 1
    if "upstream_in_question" in scenario["expect"]:
        probe = scenario.get("probe", "")
        questions = {
            task["node_id"]: task["question"] or ""
            for task in store.list_node_tasks(workflow_id)
        }
        observed["upstream_in_question"] = probe in questions.get(scenario.get("upstream_node", ""), "")

    passed = True
    for key, value in scenario["expect"].items():
        if key == "final_output_contains":
            continue
        if observed.get(key) != value:
            passed = False
    return Result(scenario["id"], scenario["category"], scenario["name"], passed, scenario["expect"], observed)


def run_governance(scenario: dict, tmp: Path) -> Result:
    check = scenario["check"]
    expect = scenario["expect"]
    observed: dict = {}
    note = ""

    if check == "idempotent_step":
        db_path = db_path_for(tmp, scenario["id"])
        store = SQLiteWorkflowStore(db_path)
        workflow_id = store.create_workflow("幂等", {"nodes": [{"id": "A"}]}, status="RUNNING")
        task_id = store.create_node_task(workflow_id, "A", "q", [])
        seed_step(db_path, task_id, 1, 10, 5)
        try:
            seed_step(db_path, task_id, 1, 10, 5)
            observed["duplicate_rejected"] = False
        except sqlite3.IntegrityError:
            observed["duplicate_rejected"] = True
        observed["rows"] = len(store.list_node_tasks(workflow_id))

    elif check == "no_progress":
        detector = NoProgressDetector(max_no_progress_rounds=scenario.get("rounds", 2))
        rounds = scenario["rounds_observations"]
        triggered = [detector.observe(r) for r in rounds]
        observed["triggered"] = triggered
        observed["final_triggered"] = triggered[-1]

    elif check == "terminal_transition":
        entity = {"status": scenario["from_status"]}
        try:
            transition(entity, scenario["to_status"])
            observed["raised"] = False
            observed["status"] = entity["status"]
        except InvalidTransitionError:
            observed["raised"] = True
            observed["status"] = entity["status"]

    elif check == "cancel_task":
        db_path = db_path_for(tmp, scenario["id"])
        store = SQLiteWorkflowStore(db_path)
        workflow_id = store.create_workflow("取消", {"nodes": [{"id": "A"}]}, status="RUNNING")
        task_id = store.create_node_task(workflow_id, "A", "q", [])
        result = cancel_task(task_id, db_path=db_path)
        observed["status"] = result["status"]
        observed["task_status"] = task_statuses(store, workflow_id)["A"]

    else:
        raise ValueError(f"未知的 governance check：{check}")

    passed = all(observed.get(key) == value for key, value in expect.items())
    return Result(scenario["id"], scenario["category"], scenario["name"], passed, expect, observed, note)


def run_cost(scenario: dict, tmp: Path) -> Result:
    check = scenario["check"]
    expect = scenario["expect"]
    observed: dict = {}

    if check == "budget_boundary":
        cases = []
        for item in scenario["cases"]:
            result = check_budget(item["spent"], item["budget"])
            cases.append(result["exceeded"])
        observed["exceeded"] = cases

    elif check == "estimate_monotonic":
        base = estimate_cost(DEFAULT_MODEL, scenario["input_tokens"], scenario["output_tokens"])
        bigger = estimate_cost(
            DEFAULT_MODEL, scenario["input_tokens"] * 2, scenario["output_tokens"] * 2
        )
        observed["monotonic"] = bigger > base
        observed["base_usd"] = round(base, 8)

    elif check == "workflow_cost":
        db_path = db_path_for(tmp, scenario["id"])
        store = SQLiteWorkflowStore(db_path)
        workflow_id = store.create_workflow("成本", {"nodes": [{"id": "A"}]}, status="RUNNING")
        task_a = store.create_node_task(workflow_id, "A", "q", [])
        task_b = store.create_node_task(workflow_id, "B", "q", [])
        seed_step(db_path, task_a, 1, scenario["tokens"]["a"][0], scenario["tokens"]["a"][1])
        seed_step(db_path, task_b, 1, scenario["tokens"]["b"][0], scenario["tokens"]["b"][1])
        cost = workflow_cost(db_path, workflow_id, DEFAULT_MODEL)
        observed["input_tokens"] = cost["input_tokens"]
        observed["output_tokens"] = cost["output_tokens"]

    elif check == "trace_attempts":
        db_path = db_path_for(tmp, scenario["id"])
        store = SQLiteWorkflowStore(db_path)
        workflow_id = store.create_workflow("轨迹", {"nodes": [{"id": "A"}]}, status="RUNNING")
        task_id = store.create_node_task(workflow_id, "A", "q", [])
        seed_step(db_path, task_id, 1, 10, 5)
        trace = build_trace(store, workflow_id, DEFAULT_MODEL)
        observed["attempts"] = len(trace["attempts"])
        observed["has_critical_path"] = "critical_path" in trace
        missing = build_trace(store, "wf-does-not-exist", DEFAULT_MODEL)
        observed["missing_workflow_none"] = missing is None

    else:
        raise ValueError(f"未知的 cost check：{check}")

    passed = all(observed.get(key) == value for key, value in expect.items())
    return Result(scenario["id"], scenario["category"], scenario["name"], passed, expect, observed)


RUNNERS = {
    "plan": run_plan,
    "schedule": simulate_workflow,
    "governance": run_governance,
    "cost": run_cost,
}


def run(scenarios: list[dict], tmp: Path) -> list[Result]:
    results = []
    for scenario in scenarios:
        runner = RUNNERS[scenario["kind"]]
        try:
            results.append(runner(scenario, tmp))
        except Exception as exc:  # noqa: BLE001 —— 评测要记录失败，不是崩掉整轮
            results.append(
                Result(
                    scenario["id"],
                    scenario["category"],
                    scenario["name"],
                    False,
                    scenario.get("expect", {}),
                    {"exception": f"{type(exc).__name__}: {exc}"},
                )
            )
    return results


def render_markdown(results: list[Result], source: Path) -> str:
    total = len(results)
    passed = sum(1 for item in results if item.passed)
    lines = [
        "# Agent Platform 场景评测报告",
        "",
        f"- 场景集：`{source.name}`（{total} 条）",
        f"- 通过：**{passed}/{total}**",
        "",
        "## 分类通过率",
        "",
        "| 分类 | 通过 / 总数 |",
        "| --- | --- |",
    ]
    categories = sorted({item.category for item in results})
    for category in categories:
        items = [item for item in results if item.category == category]
        lines.append(f"| {category} | {sum(1 for i in items if i.passed)} / {len(items)} |")
    lines += [
        "",
        "## 场景明细",
        "",
        "| 场景 | 分类 | 结果 | 期望 | 实测 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in results:
        mark = "✅" if item.passed else "❌"
        expected = json.dumps(item.expected, ensure_ascii=False)
        observed = json.dumps(item.observed, ensure_ascii=False)
        lines.append(f"| {item.name} | {item.category} | {mark} | `{expected[:160]}` | `{observed[:200]}` |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Platform 场景评测")
    parser.add_argument("--category", help="只跑某一类：plan / schedule / governance / cost")
    parser.add_argument("--report", help="把 markdown 报告写到指定路径")
    parser.add_argument("--check", action="store_true", help="有失败场景时返回退出码 1")
    parser.add_argument("--json", help="把逐场景结果写成 JSON")
    args = parser.parse_args()

    scenarios = load_scenarios(SCENARIOS, args.category)
    with tempfile.TemporaryDirectory(prefix="agent-platform-evals-") as tmpdir:
        results = run(scenarios, Path(tmpdir))

    report = render_markdown(results, SCENARIOS)
    print(report)
    if args.report:
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report, encoding="utf-8")
        print(f"报告已写入：{target}")
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps([item.as_row() for item in results], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    failed = [item for item in results if not item.passed]
    if failed and args.check:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
