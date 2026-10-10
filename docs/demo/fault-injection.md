# 故障注入实验（agent-platform）

> 真实容器栈（postgres + redis + api + orchestrator + worker）+ 真实 DeepSeek API；运行时间 2026-10-10 08:50 UTC。
> 复现：`python3 scripts/demo/fault_injection.py --scenario all`

## worker-kill

**注入**：workflow 执行到一半时 `docker compose kill worker`。

- kill 前 RUNNING 任务数：1；kill 后仍为 1（任务不会自己恢复）
- 恢复手段：把 orchestrator 用 `STALE_TASK_MINUTES=1` 拉起，让它回收僵尸任务并重派
- 终态：**COMPLETED**，恢复耗时 63.6 s
- 被重试的节点：['beijing_pop']

| 节点 | 第几次尝试 | 状态 | duration_ms |
| --- | --- | --- | --- |
| beijing_pop | 1 | FAILED | None |
| shanghai_pop | 1 | COMPLETED | 7781 |
| beijing_pop | 2 | COMPLETED | 6690 |
| compare_summary | 1 | COMPLETED | 5222 |

**结论**：worker 崩掉不会丢状态；跑在 DB 里的状态让任务可被回收 + 重试，节点级重试上限兜底。

## redis-restart

**注入**：先停 worker（让任务堆在 Redis 队列），再 `docker compose kill redis` 模拟非正常宕机。

- 重启前：队列里 4 条消息，DB 里 4 个 PENDING 任务
- 重启后：队列里 4 条消息，PENDING 4 个、RUNNING 0 个
- 是否搁浅：**True**（Redis 里的消息没了，DB 里的 PENDING 任务没人再派发）
- 打开孤儿 PENDING 回收后：39.4 s 内被重新入队并跑完，终态 ['COMPLETED', 'COMPLETED']

**结论**：队列是"尽力而为"的投递层，状态才是事实来源；只回收 RUNNING 不够，PENDING 消息丢了同样会让流水线永久停住 —— 需要按 DB 状态做补偿重入队。

## budget-gate

**注入**：`budget_usd=0.0002` 提交 workflow（远低于一次正常执行的开销）。

- 终态：**FAILED**（超支是确定性失败，不进重试）
- 任务状态：['beijing_population:BUDGET_EXCEEDED:1', 'shanghai_population:BUDGET_EXCEEDED:1']
- 成本：输入 567 token / 输出 99 token，已花 $0.000262，预算 $0.0002
- 尝试次数：2 次（没有重试）

**结论**：闸门在 LLM 调用**之前**生效，超预算立即停手并落终态，而不是事后统计。

## 历史运行（自动汇总）

| 运行时间 | 场景 | 关键结果 |
| --- | --- | --- |
| 20261010-084101 | worker-kill | kill 后 RUNNING=1，66.7s 后终态 COMPLETED，重试节点 ['research_beijing'] |
| 20261010-084101 | redis-restart | 重启后 PENDING=2/RUNNING=1，搁浅=False |
| 20261010-084101 | budget-gate | 终态 FAILED，已花 $0.0003 / 预算 $0.0002 |
| 20261010-084216 | redis-restart | 重启后 PENDING=4/RUNNING=0，搁浅=True |
| 20261010-085035 | worker-kill | kill 后 RUNNING=1，63.6s 后终态 COMPLETED，重试节点 ['beijing_pop'] |
| 20261010-085035 | redis-restart | 重启后 PENDING=4/RUNNING=0，搁浅=True，39.4s 后自愈=True |
| 20261010-085035 | budget-gate | 终态 FAILED，已花 $0.000262 / 预算 $0.0002 |
