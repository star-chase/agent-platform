# docs/demo —— 可复现的演示与故障注入素材

这里的东西都是**真跑出来的**：真实容器栈（postgres + redis + api + orchestrator + worker）+ 真实模型调用。
脚本在 [`scripts/demo/`](../../scripts/demo/)，产物就是下面的文件。

| 素材 | 说明 | 头条数字 |
| --- | --- | --- |
| [`cost-trace.md`](cost-trace.md) | 一条三节点 workflow 的 `/cost` 与 `/trace` 原始响应 + Mermaid 时间线 | 6382 入 + 3478 出 token，$0.005393（预算 $0.05）；关键路径 12.9 s；最慢节点 7.5 s 里 7.4 s 是模型延迟 |
| [`loadtest.md`](loadtest.md) | 6 条 workflow 并发，对比 1 个与 3 个 worker | 119.8 s → 45.7 s；吞吐 3.0 → 7.9 条/分钟（2.6×）；成本几乎不变（$0.0318 → $0.0307） |
| [`fault-injection.md`](fault-injection.md) | 故障注入三连：kill worker、kill Redis、极小预算 | worker 被 kill 后 63.6 s 回收+重试成功；Redis 丢消息导致 4 个 PENDING 搁浅，补偿重投后 39.4 s 自愈；预算闸门终态 FAILED 且不重试 |

原始数据（JSON）一并放在同目录，便于对比多次运行：
`cost-*.json`、`trace.json`、`loadtest-worker-*.json`、`fault-injection-*.json`。

## 复现

```bash
docker compose up -d

python3 scripts/demo/capture_cost_trace.py
python3 scripts/demo/loadtest.py --label worker-1 --workflows 6 --workers 1
docker compose up -d --scale worker=3 --no-deps worker
python3 scripts/demo/loadtest.py --label worker-3 --workflows 6 --workers 3
docker compose up -d --scale worker=1 --no-deps worker

python3 scripts/demo/fault_injection.py --scenario all
```

> 会花真实 API 额度：一条三节点 workflow 约 $0.005，压测 6 条约 $0.03。
> 故障注入会真的 kill / restart 容器，脚本结束时会恢复 orchestrator 与 worker 的正常配置。
