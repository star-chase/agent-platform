#!/usr/bin/env python3
"""抓取 /cost 与 /trace 的真实数据，生成 docs/demo 下的演示素材。

用法（容器栈已起来）：
    python3 scripts/demo/capture_cost_trace.py
    python3 scripts/demo/capture_cost_trace.py --goal "..." --budget 0.05 --out docs/demo
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


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


def wait_terminal(base: str, workflow_id: str, timeout: int) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        workflow = api_get(base, f"/workflows/{workflow_id}")["workflow"]
        if workflow["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            return workflow
        time.sleep(3)
    raise TimeoutError(f"workflow {workflow_id} 在 {timeout}s 内没有进入终态")


def mermaid_timeline(trace: dict) -> str:
    """把 attempts 画成 gantt：一眼看出并行、关键路径与每步耗时。"""
    lines = [
        "```mermaid",
        "gantt",
        "    title 节点时间线（由 /trace 的 attempts 生成）",
        "    dateFormat YYYY-MM-DD HH:mm:ss",
        "    axisFormat %H:%M:%S",
    ]
    for attempt in trace["attempts"]:
        started = (attempt.get("started_at") or "")[:19]
        if not started:
            continue
        duration = max(round((attempt.get("duration_ms") or 0) / 1000), 1)
        label = f"{attempt['node_id']} #{attempt.get('attempt', 1)} {attempt['status']}"
        lines.append(f"    section {attempt['node_id']}")
        lines.append(f"    {label} :done, {attempt['task_id'][:8]}, {started}, {duration}s")
    lines.append("```")
    return "\n".join(lines)


def render_capture(workflow: dict, cost: dict, trace: dict) -> str:
    slowest = trace.get("slowest_node") or {}
    llm_share = (
        round(100 * (slowest.get("llm_ms") or 0) / slowest["duration_ms"], 1)
        if slowest.get("duration_ms")
        else 0
    )
    rows = "\n".join(
        f"| {node} | {item['attempts']} | {item['status']} | {item['duration_ms']} | "
        f"{item.get('llm_ms')} | {item['steps']} | {item['input_tokens']} + {item['output_tokens']} | "
        f"${item['cost_usd']:.6f} |"
        for node, item in ((n["node_id"], n) for n in trace["nodes"])
    )
    return f"""# /cost 与 /trace 演示素材

> 数据来自 `python3 scripts/demo/capture_cost_trace.py` 真实跑出来的一条 workflow（模型：`{cost['model']}`）。
> 抓取时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

## 1. 这条 workflow

- 目标：{workflow['goal']}
- workflow_id：`{workflow['id']}`
- 状态：**{workflow['status']}**，总耗时 `{trace.get('duration_ms')} ms`
- DAG 节点：{", ".join(node['id'] for node in workflow['dag']['nodes'])}

## 2. `GET /workflows/{{id}}/cost` —— 花了多少钱

```json
{json.dumps(cost, ensure_ascii=False, indent=2)}
```

- 预算 ${cost['budget_usd']}，实际 ${cost['cost_usd']}（{100 * cost['cost_usd'] / cost['budget_usd']:.1f}% 用掉）
- token：{cost['input_tokens']} 输入 + {cost['output_tokens']} 输出

## 3. `GET /workflows/{{id}}/trace` —— 时间花在哪一步

```json
{json.dumps({k: trace[k] for k in ("workflow_id", "status", "duration_ms", "critical_path", "critical_path_ms", "slowest_node", "most_tokens_node")}, ensure_ascii=False, indent=2)}
```

### 每个节点的分项

| 节点 | 尝试次数 | 状态 | duration_ms | llm_ms | steps | token（入 + 出） | 成本 |
| --- | --- | --- | --- | --- | --- | --- | --- |
{rows}

### 关键结论

- 关键路径 `{" → ".join(trace['critical_path'])}`，{trace['critical_path_ms']} ms；总耗时 {trace.get('duration_ms')} ms。
- 最慢节点 `{slowest.get('node_id')}`：{slowest.get('duration_ms')} ms 里 {slowest.get('llm_ms')} ms 是模型延迟（**{llm_share}%**）——
  这正是把 `llm_ms` 和 `duration_ms` 分开记账的意义：慢在模型还是慢在工具，一眼能看出来。
- 成本最大的节点：`{(trace.get('most_tokens_node') or {}).get('node_id')}`。

{mermaid_timeline(trace)}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 /cost 与 /trace 演示素材")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--goal", default="分别查一下北京和上海的人口，然后写一段对比总结")
    parser.add_argument("--budget", type=float, default=0.05)
    parser.add_argument("--out", default="docs/demo")
    parser.add_argument("--timeout", type=int, default=420)
    args = parser.parse_args()

    created = api_post(args.api, "/workflows", {"goal": args.goal, "budget_usd": args.budget})
    workflow_id = created["workflow_id"]
    print(f"workflow {workflow_id} 已提交，等待终态…")
    workflow = wait_terminal(args.api, workflow_id, args.timeout)
    cost = api_get(args.api, f"/workflows/{workflow_id}/cost")
    trace = api_get(args.api, f"/workflows/{workflow_id}/trace")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "cost.json").write_text(json.dumps(cost, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "cost-trace.md").write_text(render_capture(workflow, cost, trace), encoding="utf-8")
    # 当前运行的这条额外存一份，方便对比多次抓取
    (out / f"cost-{workflow_id[:8]}.json").write_text(
        json.dumps(cost, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"状态 {workflow['status']}｜耗时 {trace.get('duration_ms')} ms｜"
          f"成本 ${cost['cost_usd']:.6f}（预算 ${cost['budget_usd']}）")
    print(f"素材已写入 {out}/cost-trace.md")
    return 0 if workflow["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
