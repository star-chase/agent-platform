# Agent Platform 场景评测（evals）

单元测试测**边界**，evals 测**场景**：把一次完整运行会经历的东西（规划 → 调度 → 重试 → 治理 → 成本 → 轨迹）
用真实组件跑一遍，产出一张可对比的评分卡。

- 真实组件：`SQLiteWorkflowStore`、`WorkflowScheduler`、`app/approvals.py`、`app/cost.py`、
  `app/trace.py`、`app/state_machine.py`、`app/progress_guard.py`
- 脚本替身只有两处：**模型输出**（Planner 的计划 JSON）和 **worker 执行结果**（节点成功 / 失败 / 超预算）
- 因此整套评测**离线可跑**：不需要网络、不需要数据库服务、不需要 Redis

## 怎么跑

```bash
python -m evals.run_evals                        # 跑全部场景，打印 markdown 摘要
python -m evals.run_evals --report evals/report.md
python -m evals.run_evals --check                # 有失败场景时退出码 1（CI 用）
python -m evals.run_evals --category governance  # 只跑某一类
python -m evals.run_evals --json evals/result.json
```

## 场景集（24 条）

| 分类 | 条数 | 覆盖的场景 |
| --- | --- | --- |
| `plan` | 6 | 菱形 / 链式 / 一对多计划的节点与依赖；环、未知依赖、超节点上限被规划期拒绝 |
| `schedule` | 9 | 终态与最终产出、并行派发宽度、上游产出注入下游问题、失败重试成功、重试耗尽、预算超支不重试、审批通过 / 拒绝、worker 卡死时调度器的行为 |
| `governance` | 5 | `(task_id, iteration)` 幂等、无进展检测触发与复位、终态不可回到 RUNNING、取消任务 |
| `cost` | 4 | 预算闸门边界（用满即停 / 无上限不拦）、成本估算单调性、workflow 成本汇总、trace 与未知 workflow |

## 场景怎么写

`evals/scenarios.jsonl` 一行一个场景，按 `kind` 决定字段：

```jsonc
// kind=plan：验证计划解析成 DAG 的结果
{"id": "...", "category": "plan", "kind": "plan", "name": "...",
 "plan": {"nodes": [{"id": "A"}, {"id": "B", "depends_on": ["A"]}]},
 "expect": {"ok": true, "nodes": ["A", "B"], "edges": [["A", "B"]], "sinks": ["B"],
            "ready_after": {"completed": ["A"], "ready": ["B"]}}}

// kind=schedule：脚本化跑一条 workflow（script = 每个节点每次尝试的结果）
{"id": "...", "category": "schedule", "kind": "schedule", "name": "...",
 "dag": {"nodes": [{"id": "A"}, {"id": "B", "depends_on": ["A"]}]},
 "script": {"A": ["COMPLETED"], "B": ["FAILED", "COMPLETED"]},
 "approvals": {"B": "reject"},          // 可选：requires_approval 节点的决定
 "expect": {"terminal": "COMPLETED", "attempts": {"A": 1, "B": 2}}}

// kind=governance / kind=cost：check 指定用哪条不变量跑
{"id": "...", "kind": "governance", "check": "idempotent_step", "expect": {"duplicate_rejected": true}}
```

## 这套评测不测什么

模型与 worker 是脚本替身，所以 evals **不衡量模型质量**，衡量的是平台的语义是否还成立：
调度是否正确、重试与终态是否符合设计、治理闸门是否有效、成本与轨迹是否算得对。
模型质量靠真实 key 的端到端脚本（`python tests/test_run_persistent.py`）与线上 trace 看。

## CI

`.github/workflows/ci.yml` 在每次 push / PR 跑：`python -m pytest -q` + `python -m evals.run_evals --check`，
并把 `evals/report.md` 作为 artifact 上传。
