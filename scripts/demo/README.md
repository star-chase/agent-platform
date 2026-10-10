# scripts/demo —— 演示与故障注入

这三个脚本把"接口能返回什么"和"出事时系统怎么表现"变成可以复现、可以截图/贴进简历的素材。
产物统一写到 `docs/demo/`。

前置：容器栈已经起来（`docker compose up -d`），`.env.docker` 里配好 `DEEPSEEK_API_KEY`。

| 脚本 | 做什么 | 产物 |
| --- | --- | --- |
| `capture_cost_trace.py` | 真跑一条 workflow，抓 `/cost` 与 `/trace`，生成带 Mermaid 时间线的演示文档 | `docs/demo/cost-trace.md`、`cost.json`、`trace.json` |
| `loadtest.py` | 并发提交 N 条 workflow，统计吞吐 / p50 / p95 / 成本，用于对比 1 个和 3 个 worker | `docs/demo/loadtest.md`、`loadtest-<label>.json` |
| `fault_injection.py` | 故障注入三连：kill worker、重启 Redis、极小预算触发闸门 | `docs/demo/fault-injection.md`、`fault-injection-<时间戳>.json` |

## 用法

```bash
# 1) /cost 与 /trace 素材
python3 scripts/demo/capture_cost_trace.py

# 2) 压测：先 1 个 worker，再扩到 3 个
python3 scripts/demo/loadtest.py --label worker-1 --workflows 6 --workers 1
docker compose up -d --scale worker=3 --no-deps worker
python3 scripts/demo/loadtest.py --label worker-3 --workflows 6 --workers 3
docker compose up -d --scale worker=1 --no-deps worker

# 3) 故障注入（会真的 kill / restart 容器，跑完自动恢复）
python3 scripts/demo/fault_injection.py --scenario all
```

## 注意

- 这些脚本会花真实的 API 额度：一条三节点 workflow 约 `$0.005`，压测 6 条约 `$0.03`。
- `compose.fault.yml` 把 `STALE_TASK_MINUTES` 调成 1 分钟、`ORPHAN_TASK_RECOVERY_SECONDS` 调成 10 秒，
  只是为了在几分钟内看到恢复动作；生产阈值应该是分钟级（默认 `STALE_TASK_MINUTES=15`）。
- 故障注入期间 orchestrator / worker 会被重建，实验结束脚本会把它们恢复正常配置。
