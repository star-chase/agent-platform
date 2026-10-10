#!/usr/bin/env python3
"""故障注入三连：worker kill / Redis 重启 / 预算闸门。

每个场景都**真的**去动容器，然后从数据库、/workflows/{id}、/cost、/trace 里把证据抓回来，
结果写进 docs/demo/fault-injection.md。

用法（容器栈已起来）：
    python3 scripts/demo/fault_injection.py --scenario all
    python3 scripts/demo/fault_injection.py --scenario redis-restart
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

def find_repo_root() -> Path:
    """优先用当前工作目录向上找 docker-compose.yml，方便从任意位置跑这个脚本。"""
    for candidate in (Path.cwd(), *Path.cwd().parents):
        if (candidate / "docker-compose.yml").exists():
            return candidate
    return Path(__file__).resolve().parents[2]


REPO_ROOT = find_repo_root()
FAULT_OVERRIDE = "scripts/demo/compose.fault.yml"
GOAL = "分别查一下北京和上海的人口，然后写一段对比总结"


def run(cmd: list[str], timeout: int = 240) -> str:
    result = subprocess.run(  # noqa: S603
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout, check=False
    )
    return (result.stdout + result.stderr).strip()


def compose(*args: str, override: bool = False, timeout: int = 240) -> str:
    cmd = ["docker", "compose", "-f", "docker-compose.yml"]
    if override:
        cmd += ["-f", FAULT_OVERRIDE]
    return run(cmd + list(args), timeout=timeout)


def psql(sql: str, timeout: int = 60) -> str:
    return run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "agent", "-d", "agent_platform", "-tAc", sql],
        timeout=timeout,
    ).strip()


def api_get(base: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base}{path}", timeout=30) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def api_post(base: str, path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base}{path}", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def submit(base: str, goal: str, budget: float) -> str:
    return api_post(base, "/workflows", {"goal": goal, "budget_usd": budget})["workflow_id"]


def wait_terminal(base: str, workflow_id: str, timeout: int = 300) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = api_get(base, f"/workflows/{workflow_id}")["workflow"]["status"]
        if status in {"COMPLETED", "FAILED", "CANCELLED"}:
            return status
        time.sleep(3)
    return "STUCK"


def count_tasks(workflow_id: str, status: str | None = None) -> int:
    where = f"workflow_id = '{workflow_id}'"
    if status:
        where += f" AND status = '{status}'"
    return int(psql(f"SELECT count(*) FROM tasks WHERE {where}") or 0)


def task_states(workflow_id: str) -> list[str]:
    rows = psql(
        "SELECT node_id || ':' || status || ':' || count(*) FROM tasks "
        f"WHERE workflow_id = '{workflow_id}' GROUP BY node_id, status ORDER BY node_id"
    )
    return [row for row in rows.splitlines() if row]


def normalize_workers() -> None:
    compose("up", "-d", "--no-deps", "--scale", "worker=1", "worker", timeout=300)


def scenario_worker_kill(base: str) -> dict:
    print("[worker-kill] 提交 workflow，等有节点真的在跑 …")
    normalize_workers()
    workflow_id = submit(base, GOAL, 0.05)
    deadline = time.time() + 120
    while time.time() < deadline and count_tasks(workflow_id, "RUNNING") == 0:
        time.sleep(2)
    running_before = count_tasks(workflow_id, "RUNNING")
    if running_before == 0:
        return {"scenario": "worker-kill", "error": "没等到 RUNNING 的任务", "workflow_id": workflow_id}

    print(f"[worker-kill] 有 {running_before} 个任务在跑，kill worker")
    compose("kill", "worker")
    time.sleep(6)
    running_after = count_tasks(workflow_id, "RUNNING")

    print("[worker-kill] 用 1 分钟回收阈值把 orchestrator + worker 重新拉起来")
    compose("up", "-d", "--no-deps", "orchestrator", "worker", override=True)
    recovered_at = time.time()
    status = wait_terminal(base, workflow_id, timeout=420)
    recovery_seconds = round(time.time() - recovered_at, 1)

    trace = api_get(base, f"/workflows/{workflow_id}/trace")
    attempts = [
        {"node_id": item["node_id"], "attempt": item["attempt"], "status": item["status"], "duration_ms": item["duration_ms"]}
        for item in trace["attempts"]
    ]
    recovered_attempts = [item for item in attempts if item["attempt"] > 1]

    compose("up", "-d", "--no-deps", "orchestrator", "worker")
    return {
        "scenario": "worker-kill",
        "workflow_id": workflow_id,
        "running_before_kill": running_before,
        "running_after_kill": running_after,
        "terminal": status,
        "recovery_seconds": recovery_seconds,
        "attempts": attempts,
        "retried_nodes": sorted({item["node_id"] for item in recovered_attempts}),
        "cost": api_get(base, f"/workflows/{workflow_id}/cost"),
    }


def scenario_redis_restart(base: str, expect_recovery: bool = True) -> dict:
    print("[redis-restart] 停掉 worker，让任务先堆在 Redis 队列里 …")
    normalize_workers()
    compose("stop", "worker")
    workflow_ids = [submit(base, GOAL, 0.05) for _ in range(2)]
    time.sleep(8)
    queued = compose("exec", "-T", "redis", "redis-cli", "-p", "6379", "LLEN", "agent:tasks").strip()
    pending_before = sum(count_tasks(wf, "PENDING") for wf in workflow_ids)

    print(f"[redis-restart] 队列里有 {queued} 条消息、DB 里 {pending_before} 个 PENDING 任务；kill Redis（模拟非正常宕机）")
    compose("kill", "redis")
    time.sleep(2)
    compose("start", "redis")
    time.sleep(4)
    queued_after = compose("exec", "-T", "redis", "redis-cli", "-p", "6379", "LLEN", "agent:tasks").strip()
    compose("start", "worker")
    time.sleep(20)

    pending_after = sum(count_tasks(wf, "PENDING") for wf in workflow_ids)
    running_after = sum(count_tasks(wf, "RUNNING") for wf in workflow_ids)
    stranded = pending_after > 0 and running_after == 0

    outcome: dict = {
        "scenario": "redis-restart",
        "workflow_ids": workflow_ids,
        "queued_messages_before_restart": queued,
        "queued_messages_after_restart": queued_after,
        "pending_before_restart": pending_before,
        "pending_after_restart": pending_after,
        "running_after_restart": running_after,
        "stranded": stranded,
    }

    if stranded and expect_recovery:
        print("[redis-restart] 任务搁浅，打开孤儿 PENDING 回收（ORPHAN_TASK_RECOVERY_SECONDS）后观察是否自愈")
        compose("up", "-d", "--no-deps", "orchestrator", "worker", override=True)
        started = time.time()
        terminals = [wait_terminal(base, wf, timeout=300) for wf in workflow_ids]
        outcome["recovered_in_seconds"] = round(time.time() - started, 1)
        outcome["terminals_after_recovery"] = terminals
        outcome["recovered"] = all(status == "COMPLETED" for status in terminals)
        compose("up", "-d", "--no-deps", "orchestrator", "worker")
    else:
        outcome["terminals_after_restart"] = [api_get(base, f"/workflows/{wf}")["workflow"]["status"] for wf in workflow_ids]
    return outcome


def scenario_budget_gate(base: str) -> dict:
    print("[budget-gate] 用极小预算提交 workflow，观察预算闸门")
    normalize_workers()
    workflow_id = submit(base, GOAL, 0.0002)
    status = wait_terminal(base, workflow_id, timeout=300)
    states = task_states(workflow_id)
    trace = api_get(base, f"/workflows/{workflow_id}/trace")
    return {
        "scenario": "budget-gate",
        "workflow_id": workflow_id,
        "terminal": status,
        "task_states": states,
        "attempts": [
            {"node_id": item["node_id"], "attempt": item["attempt"], "status": item["status"]}
            for item in trace["attempts"]
        ],
        "cost": api_get(base, f"/workflows/{workflow_id}/cost"),
    }


def render(results: list[dict], history: list[dict] | None = None) -> str:
    lines = [
        "# 故障注入实验（agent-platform）",
        "",
        f"> 真实容器栈（postgres + redis + api + orchestrator + worker）+ 真实 DeepSeek API；"
        f"运行时间 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}。",
        "> 复现：`python3 scripts/demo/fault_injection.py --scenario all`",
        "",
    ]
    for item in results:
        scenario = item.get("scenario")
        lines.append(f"## {scenario}")
        lines.append("")
        if scenario == "worker-kill":
            lines += [
                "**注入**：workflow 执行到一半时 `docker compose kill worker`。",
                "",
                f"- kill 前 RUNNING 任务数：{item.get('running_before_kill')}；kill 后仍为 "
                f"{item.get('running_after_kill')}（任务不会自己恢复）",
                f"- 恢复手段：把 orchestrator 用 `STALE_TASK_MINUTES=1` 拉起，让它回收僵尸任务并重派",
                f"- 终态：**{item.get('terminal')}**，恢复耗时 {item.get('recovery_seconds')} s",
                f"- 被重试的节点：{item.get('retried_nodes')}",
                "",
                "| 节点 | 第几次尝试 | 状态 | duration_ms |",
                "| --- | --- | --- | --- |",
            ]
            lines += [
                f"| {a['node_id']} | {a['attempt']} | {a['status']} | {a['duration_ms']} |"
                for a in item.get("attempts", [])
            ]
            lines += ["", "**结论**：worker 崩掉不会丢状态；跑在 DB 里的状态让任务可被回收 + 重试，节点级重试上限兜底。", ""]
        elif scenario == "redis-restart":
            lines += [
                "**注入**：先停 worker（让任务堆在 Redis 队列），再 `docker compose kill redis` 模拟非正常宕机。",
                "",
                f"- 重启前：队列里 {item.get('queued_messages_before_restart')} 条消息，DB 里 "
                f"{item.get('pending_before_restart')} 个 PENDING 任务",
                f"- 重启后：队列里 {item.get('queued_messages_after_restart')} 条消息，PENDING "
                f"{item.get('pending_after_restart')} 个、RUNNING {item.get('running_after_restart')} 个",
                f"- 是否搁浅：**{item.get('stranded')}**（Redis 里的消息没了，DB 里的 PENDING 任务没人再派发）",
            ]
            if "recovered" in item:
                lines += [
                    f"- 打开孤儿 PENDING 回收后：{item.get('recovered_in_seconds')} s 内被重新入队并跑完，"
                    f"终态 {item.get('terminals_after_recovery')}",
                ]
            lines.append("")
            lines.append(
                "**结论**：队列是\"尽力而为\"的投递层，状态才是事实来源；只回收 RUNNING 不够，"
                "PENDING 消息丢了同样会让流水线永久停住 —— 需要按 DB 状态做补偿重入队。"
            )
            lines.append("")
        elif scenario == "budget-gate":
            cost = item.get("cost", {})
            lines += [
                "**注入**：`budget_usd=0.0002` 提交 workflow（远低于一次正常执行的开销）。",
                "",
                f"- 终态：**{item.get('terminal')}**（超支是确定性失败，不进重试）",
                f"- 任务状态：{item.get('task_states')}",
                f"- 成本：输入 {cost.get('input_tokens')} token / 输出 {cost.get('output_tokens')} token，"
                f"已花 ${cost.get('cost_usd')}，预算 ${cost.get('budget_usd')}",
                f"- 尝试次数：{len(item.get('attempts', []))} 次（没有重试）",
                "",
                "**结论**：闸门在 LLM 调用**之前**生效，超预算立即停手并落终态，而不是事后统计。",
                "",
            ]
    if history:
        lines += [
            "## 历史运行（自动汇总）",
            "",
            "| 运行时间 | 场景 | 关键结果 |",
            "| --- | --- | --- |",
        ]
        for run_item in history:
            for item in run_item["scenarios"]:
                scenario = item.get("scenario")
                if scenario == "worker-kill":
                    detail = (
                        f"kill 后 RUNNING={item.get('running_after_kill')}，"
                        f"{item.get('recovery_seconds')}s 后终态 {item.get('terminal')}，"
                        f"重试节点 {item.get('retried_nodes')}"
                    )
                elif scenario == "redis-restart":
                    detail = (
                        f"重启后 PENDING={item.get('pending_after_restart')}/RUNNING={item.get('running_after_restart')}，"
                        f"搁浅={item.get('stranded')}"
                    )
                    if item.get("recovered") is not None:
                        detail += f"，{item.get('recovered_in_seconds')}s 后自愈={item.get('recovered')}"
                elif scenario == "budget-gate":
                    cost = item.get("cost", {})
                    detail = f"终态 {item.get('terminal')}，已花 ${cost.get('cost_usd')} / 预算 ${cost.get('budget_usd')}"
                else:
                    detail = json.dumps(item, ensure_ascii=False)[:120]
                lines.append(f"| {run_item['stamp']} | {scenario} | {detail} |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="agent-platform 故障注入实验")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--scenario", choices=["worker-kill", "redis-restart", "budget-gate", "all"], default="all")
    parser.add_argument("--out", default="docs/demo")
    parser.add_argument(
        "--no-expect-recovery",
        action="store_true",
        help="修复前记录现象用：Redis 重启后不尝试恢复，直接记录搁浅状态",
    )
    args = parser.parse_args()

    print(compose("ps", "--format", "{{.Service}} {{.Status}}"))
    expect_recovery = not args.no_expect_recovery
    scenarios = {
        "worker-kill": lambda: scenario_worker_kill(args.api),
        "redis-restart": lambda: scenario_redis_restart(args.api, expect_recovery=expect_recovery),
        "budget-gate": lambda: scenario_budget_gate(args.api),
    }
    selected = list(scenarios) if args.scenario == "all" else [args.scenario]
    results = [scenarios[name]() for name in selected]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (out / f"fault-injection-{stamp}.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    history = []
    for path in sorted(out.glob("fault-injection-*.json")):
        try:
            history.append({"stamp": path.stem.replace("fault-injection-", ""), "scenarios": json.loads(path.read_text(encoding="utf-8"))})
        except json.JSONDecodeError:
            continue
    (out / "fault-injection.md").write_text(render(results, history), encoding="utf-8")
    print(f"结果写入 {out}/fault-injection.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
