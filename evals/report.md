# Agent Platform 场景评测报告

- 场景集：`scenarios.jsonl`（24 条）
- 通过：**24/24**

## 分类通过率

| 分类 | 通过 / 总数 |
| --- | --- |
| cost | 4 / 4 |
| governance | 5 / 5 |
| plan | 6 / 6 |
| schedule | 9 / 9 |

## 场景明细

| 场景 | 分类 | 结果 | 期望 | 实测 |
| --- | --- | --- | --- | --- |
| 菱形计划：节点、依赖、就绪顺序正确 | plan | ✅ | `{"ok": true, "nodes": ["A", "B", "C", "D"], "edges": [["A", "B"], ["A", "C"], ["B", "D"], ["C", "D"]], "sinks": ["D"], "ready_initial": ["A"], "ready_after": {"` | `{"ok": true, "nodes": ["A", "B", "C", "D"], "edges": [["A", "B"], ["A", "C"], ["B", "D"], ["C", "D"]], "sinks": ["D"], "ready_initial": ["A"], "ready_after": ["B", "C"]}` |
| 一对多计划：单轮可派发 3 个并行分支 | plan | ✅ | `{"ok": true, "sinks": ["B", "C", "D"], "ready_after": {"completed": ["A"], "ready": ["B", "C", "D"]}}` | `{"ok": true, "nodes": ["A", "B", "C", "D"], "edges": [["A", "B"], ["A", "C"], ["A", "D"]], "sinks": ["B", "C", "D"], "ready_initial": ["A"], "ready_after": ["B", "C", "D"]}` |
| 链式计划：每轮只有一个就绪节点 | plan | ✅ | `{"ok": true, "sinks": ["C"], "ready_initial": ["A"], "ready_after": {"completed": ["A"], "ready": ["B"]}}` | `{"ok": true, "nodes": ["A", "B", "C"], "edges": [["A", "B"], ["B", "C"]], "sinks": ["C"], "ready_initial": ["A"], "ready_after": ["B"]}` |
| 环计划被拒绝（规划期拦截，不浪费执行） | plan | ✅ | `{"ok": false}` | `{"ok": false, "error": "PlannerError"}` |
| 引用不存在节点的计划被拒绝 | plan | ✅ | `{"ok": false}` | `{"ok": false, "error": "PlannerError"}` |
| 节点数超过上限的计划被拒绝 | plan | ✅ | `{"ok": false}` | `{"ok": false, "error": "PlannerError"}` |
| 线性 DAG：全部节点完成后 workflow 终态 COMPLETED 且产出最终答案 | schedule | ✅ | `{"terminal": "COMPLETED", "attempts": {"A": 1, "B": 1}, "final_output_contains": "B-out"}` | `{"terminal": "COMPLETED", "workflow_status": "COMPLETED", "attempts": {"A": 1, "B": 1}, "max_dispatch_width": 1, "final_output": "B-out-0", "task_status": {"A": "COMPLETED", "B": "COMPLETED"}, "final_` |
| 菱形 DAG：中间两个分支在同一轮派发（并行宽度 2） | schedule | ✅ | `{"terminal": "COMPLETED", "max_dispatch_width": 2, "attempts": {"A": 1, "B": 1, "C": 1, "D": 1}}` | `{"terminal": "COMPLETED", "workflow_status": "COMPLETED", "attempts": {"A": 1, "B": 1, "C": 1, "D": 1}, "max_dispatch_width": 2, "final_output": "D-out-0", "task_status": {"A": "COMPLETED", "B": "COMP` |
| 下游节点的 question 注入直接上游产出 | schedule | ✅ | `{"terminal": "COMPLETED", "upstream_in_question": true}` | `{"terminal": "COMPLETED", "workflow_status": "COMPLETED", "attempts": {"A": 1, "B": 1}, "max_dispatch_width": 1, "final_output": "B-out-0", "task_status": {"A": "COMPLETED", "B": "COMPLETED"}, "upstre` |
| 节点失败后重试成功：workflow 仍判 COMPLETED（最新一次尝试说了算） | schedule | ✅ | `{"terminal": "COMPLETED", "attempts": {"A": 1, "B": 2}, "task_status": {"A": "COMPLETED", "B": "COMPLETED"}}` | `{"terminal": "COMPLETED", "workflow_status": "COMPLETED", "attempts": {"A": 1, "B": 2}, "max_dispatch_width": 1, "final_output": "B-out-1", "task_status": {"A": "COMPLETED", "B": "COMPLETED"}}` |
| 节点重试额度用尽：workflow 判 FAILED，尝试次数卡在上限 | schedule | ✅ | `{"terminal": "FAILED", "attempts": {"A": 1, "B": 3}}` | `{"terminal": "FAILED", "workflow_status": "FAILED", "attempts": {"A": 1, "B": 3}, "max_dispatch_width": 1, "final_output": null, "task_status": {"A": "COMPLETED", "B": "FAILED"}}` |
| 预算超支是确定性失败：整个 workflow FAILED 且不重试 | schedule | ✅ | `{"terminal": "FAILED", "attempts": {"A": 1}, "task_status": {"A": "BUDGET_EXCEEDED"}}` | `{"terminal": "FAILED", "workflow_status": "FAILED", "attempts": {"A": 1}, "max_dispatch_width": 1, "final_output": null, "task_status": {"A": "BUDGET_EXCEEDED"}}` |
| 审批门控：节点先挂起等批准，批准后才入队并完成 | schedule | ✅ | `{"terminal": "COMPLETED", "waited_for_approval": true, "attempts": {"A": 1}}` | `{"terminal": "COMPLETED", "workflow_status": "COMPLETED", "attempts": {"A": 1}, "max_dispatch_width": 0, "final_output": "A-out-0", "task_status": {"A": "COMPLETED"}, "waited_for_approval": true}` |
| 审批被拒绝：节点 CANCELLED 且 workflow 判 FAILED（人类否决不走重试） | schedule | ✅ | `{"workflow_status": "FAILED", "task_status": {"A": "CANCELLED"}}` | `{"terminal": "WAITING", "workflow_status": "FAILED", "attempts": {"A": 1}, "max_dispatch_width": 0, "final_output": null, "task_status": {"A": "CANCELLED"}}` |
| worker 卡死：调度器不会自行判死（靠 STALE_TASK_MINUTES 回收） | schedule | ✅ | `{"terminal": "WAITING", "attempts": {"A": 1}}` | `{"terminal": "WAITING", "workflow_status": "RUNNING", "attempts": {"A": 1}, "max_dispatch_width": 1, "final_output": null, "task_status": {"A": "PENDING"}}` |
| 同一步骤重复写入被唯一约束拒绝（幂等） | governance | ✅ | `{"duplicate_rejected": true}` | `{"duplicate_rejected": true, "rows": 1}` |
| 连续两轮没有新信息（第 3 次相同结果）：无进展检测触发强制收尾 | governance | ✅ | `{"final_triggered": true}` | `{"triggered": [false, false, true], "final_triggered": true}` |
| 出现新信息时不误判为无进展 | governance | ✅ | `{"final_triggered": false}` | `{"triggered": [false, false, false], "final_triggered": false}` |
| 终态 BUDGET_EXCEEDED 没有出边：不可能被改回 RUNNING | governance | ✅ | `{"raised": true, "status": "BUDGET_EXCEEDED"}` | `{"raised": true, "status": "BUDGET_EXCEEDED"}` |
| 取消 PENDING 任务：状态变为 CANCELLED | governance | ✅ | `{"status": "CANCELLED", "task_status": "CANCELLED"}` | `{"status": "CANCELLED", "task_status": "CANCELLED"}` |
| 预算闸门边界：用满即停、无上限永不拦 | cost | ✅ | `{"exceeded": [true, false, false]}` | `{"exceeded": [true, false, false]}` |
| 成本估算随 token 增长而单调增长 | cost | ✅ | `{"monotonic": true}` | `{"monotonic": true, "base_usd": 0.00082}` |
| workflow 成本按所有子任务汇总（JOIN tasks） | cost | ✅ | `{"input_tokens": 1200, "output_tokens": 600}` | `{"input_tokens": 1200, "output_tokens": 600}` |
| trace 能组装尝试记录，未知 workflow 返回 None | cost | ✅ | `{"attempts": 1, "has_critical_path": true, "missing_workflow_none": true}` | `{"attempts": 1, "has_critical_path": true, "missing_workflow_none": true}` |
