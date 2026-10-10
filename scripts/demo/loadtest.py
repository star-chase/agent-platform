#!/usr/bin/env python3
"""压测：并发提交 N 条 workflow，统计端到端延迟、吞吐与成本，用于对比 worker 数量。

用法（容器栈已起来）：
    # 1 个 worker
    python3 scripts/demo/loadtest.py --label worker-1 --workflows 6

    # 扩到 3 个 worker 再跑一次（worker 无状态，可以横向扩）
    docker compose up -d --scale worker=3 --no-deps worker
    python3 scripts/demo/loadtest.py --label worker-3 --workflows 6

结果写进 docs/demo/loadtest.md（同一文件追加不同 label 的小节），并把原始数据存 JSON。
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(ratio * len(ordered))) - 1))
    return ordered[index]


def run_one(base: str, goal: str, budget: float, timeout: int, lock: threading.Lock) -> dict:
    started = time.perf_counter()
    try:
        workflow_id = api_post(base, "/workflows", {"goal": goal, "budget_usd": budget})["workflow_id"]
    except Exception as exc:  # noqa: BLE001
        return {"status": "SUBMIT_FAILED", "error": str(exc), "wall_ms": 0}

    deadline = time.time() + timeout
    status = "TIMEOUT"
    while time.time() < deadline:
        try:
            status = api_get(base, f"/workflows/{workflow_id}")["workflow"]["status"]
        except Exception:  # noqa: BLE001
            time.sleep(2)
            continue
        if status in {"COMPLETED", "FAILED", "CANCELLED"}:
            break
        time.sleep(2)

    wall_ms = (time.perf_counter() - started) * 1000
    result = {"workflow_id": workflow_id, "status": status, "wall_ms": wall_ms}
    try:
        result["cost"] = api_get(base, f"/workflows/{workflow_id}/cost")
        trace = api_get(base, f"/workflows/{workflow_id}/trace")
        result["nodes"] = len(trace["nodes"])
        result["duration_ms"] = trace.get("duration_ms")
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    with lock:
        print(f"  {workflow_id[:8]} {status} wall={wall_ms/1000:.1f}s cost=${result.get('cost', {}).get('cost_usd', 0):.6f}")
    return result


def render_section(label: str, results: list[dict], wall_s: float, workers: int) -> str:
    ok = [item for item in results if item["status"] == "COMPLETED"]
    walls = [item["wall_ms"] / 1000 for item in ok]
    costs = [item.get("cost", {}).get("cost_usd", 0.0) for item in results]
    tokens_in = sum(item.get("cost", {}).get("input_tokens", 0) for item in results)
    tokens_out = sum(item.get("cost", {}).get("output_tokens", 0) for item in results)
    return f"""### {label}（{workers} worker × {len(results)} 条 workflow）

| 指标 | 数值 |
| --- | --- |
| 成功 / 总数 | {len(ok)} / {len(results)} |
| 总墙钟时间 | {wall_s:.1f} s |
| 吞吐 | {len(results) / wall_s * 60:.1f} 条/分钟（{len(ok) / wall_s * 60:.1f} 条成功/分钟） |
| 单条端到端 p50 / p95 | {statistics.median(walls):.1f} s / {percentile(walls, 0.95):.1f} s |
| token 合计 | {tokens_in} 输入 + {tokens_out} 输出 |
| 成本合计 | ${sum(costs):.6f} |
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="agent-platform 压测")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--label", default="worker-1")
    parser.add_argument("--workflows", type=int, default=6)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--goal", default="分别查一下北京和上海的人口，然后写一段对比总结")
    parser.add_argument("--budget", type=float, default=0.05)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--out", default="docs/demo")
    args = parser.parse_args()

    print(f"并发提交 {args.workflows} 条 workflow（{args.label}）…")
    lock = threading.Lock()
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(args.workflows, 8)) as pool:
        results = list(
            pool.map(lambda _: run_one(args.api, args.goal, args.budget, args.timeout, lock), range(args.workflows))
        )
    wall_s = time.perf_counter() - started

    section = render_section(args.label, results, wall_s, args.workers)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"loadtest-{args.label}.json").write_text(
        json.dumps({"label": args.label, "workers": args.workers, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report = out / "loadtest.md"
    header = (
        "# 压测记录（agent-platform）\n\n"
        f"> 同一目标、同一预算，模型 deepseek-v4-flash；每次运行抓取 `/cost` 与 `/trace`。\n"
        f"> 更新时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
    )
    body = section + "\n"
    if report.exists():
        text = report.read_text(encoding="utf-8")
        marker = f"### {args.label}（"
        if marker in text:  # 同 label 重跑：替换旧小节
            head, _, tail = text.partition(marker)
            _, _, rest = tail.partition("\n### ")
            report.write_text(head + body + ("### " + rest if rest else ""), encoding="utf-8")
        else:
            report.write_text(text + body, encoding="utf-8")
    else:
        report.write_text(header + body, encoding="utf-8")

    print(section)
    print(f"结果写入 {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
