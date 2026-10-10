# Agent Platform —— 一个手写的多 Agent 编排平台

> 不用 LangGraph / AutoGen，从零手写 Agent 循环、DAG 调度、持久化与断点续跑。
> 目标不是"跑通一个 demo"，而是把每一层的**为什么**都踩明白：为什么这样设计、
> 换一种做法会付出什么代价、出了问题怎么定位。

[![CI](https://github.com/star-chase/agent-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/star-chase/agent-platform/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-3776ab)](requirements.txt)
[![Tests](https://img.shields.io/badge/tests-245%20passed-2ea043)](tests)
[![Evals](https://img.shields.io/badge/evals-24%20scenarios-7c3aed)](evals)

---

## 1. 它是什么

给它一句自然语言目标，它会：

```
「分别查一下北京和上海的人口，然后写一段对比总结」
        │
        ▼
   ┌─────────┐   拆解成 DAG
   │ Planner │ ──────────────────────────────┐
   └─────────┘                               │
        │                                    ▼
        │                        ┌───────────────────────┐
        │                        │  beijing_pop (并行)   │
        │                        │  shanghai_pop (并行)  │
        │                        └───────────┬───────────┘
        │                                    ▼
        │                        ┌───────────────────────┐
        │                        │   compare_summary     │
        │                        └───────────────────────┘
        ▼
   Redis 队列 → Worker（可横向扩展）→ Agent 循环（真实 LLM + 工具调用）
        │
        ▼
   每一步都落库：token / 成本 / 耗时 / 状态 —— 可查询、可恢复、可审计
```

**能回答的问题**（不是"能跑"，是"能说清楚"）：

| 问题 | 从哪看 |
|---|---|
| 这条流水线总共花了多少钱？哪个节点最贵？ | `GET /workflows/{id}/cost` |
| 哪一步最慢？慢在模型还是慢在工具？关键路径是哪条？ | `GET /workflows/{id}/trace` |
| 这个任务到底经历了什么？ | `grep <task_id> *.log`（结构化 JSON 日志） |
| 任务中途崩了怎么办？ | 状态全落库，`resume()` 从断点续跑 |
| 危险动作怎么拦？ | HITL 人工审批（静态 + Agent 主动请求两种） |
| 烧钱怎么办？ | 四层防线：迭代上限 / 无进展检测 / 超时 / **预算闸门** |

---

## 2. 快速开始

### 方式一：Docker Compose（推荐）

```bash
cp .env.docker.example .env.docker     # 填上 DEEPSEEK_API_KEY
docker compose up --build              # 起 postgres + redis + api + orchestrator + worker
# 想开 3 个消费者（并行跑 DAG 的多个分支）：
docker compose up --build --scale worker=3
```

打开 http://localhost:8000/docs 看 API。

### 方式二：本地开发（SQLite，零外部服务，测试 1 秒跑完）

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                   # 填 DEEPSEEK_API_KEY
redis-server &                          # 只有 Redis 是必需的

uvicorn app.main:app --port 8000        # 终端 1：API
python -m app.orchestrator              # 终端 2：编排
python -m app.worker                    # 终端 3：执行（可以起多个）
```

```bash
curl -X POST localhost:8000/workflows -H 'Content-Type: application/json' \
  -d '{"goal":"分别查一下北京和上海的人口，写一段对比总结","budget_usd":0.05}'
```

测试：`python -m pytest tests/ -q` → **245 passed**（不需要网络、不需要数据库、不需要 Redis）。

场景评测：`python -m evals.run_evals --check` → **24/24 场景通过**（同样离线可跑，覆盖计划校验 / 调度终态 / 治理 / 成本四类场景，报告见 `evals/report.md`）。

---

## 3. 架构

### 三个进程，各管一段

| 进程 | 职责 | 为什么单独一个进程 |
|---|---|---|
| **API**（`app/main.py`） | 收目标、调 Planner 规划、落库，**不跑长循环** | 长循环会占满请求处理资源（同步 LLM 调用几分钟） |
| **Orchestrator**（`app/orchestrator.py`） | 轮询 RUNNING 的 workflow，按 DAG 派发节点 | 需要"全局视角"，且**只能有一个实例**（否则重复派发） |
| **Worker**（`app/worker.py`） | 从队列取单个 task 执行 | **无状态、可任意横向扩展**（`--scale worker=3`） |

### 状态机

```
Task:      PENDING ──► RUNNING ──► COMPLETED
              │           │
              │           ├──► FAILED ──► RUNNING      （节点级重试，最多 3 次）
              │           ├──► WAITING_APPROVAL ──► PENDING（批准）/ CANCELLED（拒绝）
              │           └──► BUDGET_EXCEEDED          （终态，**确定性失败，不重试**）
              └──► CANCELLED

Workflow:  PLANNING ──► RUNNING ──► COMPLETED / FAILED
```

> 注意 `FAILED` **有出边**（能回到 RUNNING，重试要用），而 `BUDGET_EXCEEDED` 和
> `CANCELLED` 是终态 —— **状态的出边形状本身就是语义**：重试一次还是超支，
> 所以预算超支不该走重试。这和"人类的否决不走重试"是同一条推理。

### 目录

```
app/
  main.py              FastAPI 端点（薄：校验 + 映射错误码）
  planner.py           目标 → DAG（LLM + Function Calling）
  dag.py               DAG 数据结构与拓扑校验
  scheduler.py         纯函数：给定节点状态，算出这一轮该派发谁
  workflow_runner.py   调度器主循环（重试、审批门控、终态判定）
  orchestrator.py      轮询进程
  worker.py            队列消费循环（失败不影响进程存活）
  worker_runtime.py    任务级装配：角色档案 + 起止时间 + 事件
  agent_fc_persistent.py  Agent 循环（Function Calling + 全量落库 + 断点续跑）
  approvals.py         HITL 审批服务层（不 import Redis / FastAPI，可离线测）
  cost.py              token 计量与成本核算
  trace.py             时间线 / 关键路径（纯函数为主，可离线测）
  logging_setup.py     结构化 JSON 日志 + contextvars 上下文
  db.py                数据库访问层（SQLite / PostgreSQL 双方言适配）
  config.py            集中式配置（唯一读环境变量的地方）
```

工程配套：

```text
.github/workflows/ci.yml   单元测试 + 场景评测 + 评测报告 artifact
evals/                     场景评测：scenarios.jsonl + 评分卡（离线可跑）
docs/demo/                 演示与故障注入素材（真实跑出来的 /cost、/trace、压测、故障注入）
scripts/demo/              生成素材的脚本：capture_cost_trace / loadtest / fault_injection
```

---

## 4. 关键设计决策

### 4.1 为什么手写 Agent 循环，而不是用 LangGraph

| | 手写（本项目） | LangGraph |
|---|---|---|
| 循环 | `for iteration in range(max_iterations)` | 图上的节点 + 边 |
| 状态 | `steps` 表（每步一行） | checkpoint |
| 恢复 | `resume()` 从 `steps` 重建 messages | 从 checkpoint 恢复 |
| 代价 | 消息配对、并行工具调用等协议细节要自己扛 | 框架兜底 |

**结论**：概念是一一对应的。手写一遍之后再看框架，能看出它在替你做什么、
以及它在哪些地方替你做了你不想被替的决定（比如"历史怎么存"）。

### 4.2 为什么 Planner 和 Executor 分成两阶段

1. **上下文隔离**：规划需要全局长视野，执行只需要局部信息，混在一起两边都做不好；
2. **并行**：DAG 一次规划，多个节点可以同时跑（实测 3 worker 提速 29%）；
3. **局部重试**：一个节点失败只重跑那个节点，不用重跑整条链；
4. **可审批**：执行前能看到完整的执行计划，人类有机会说不。

### 4.3 为什么用 SSE 而不是 WebSocket

事件是**单向、只追加**的：服务端推、前端收，不需要客户端发言。
SSE 天然匹配这个形状 —— 纯 HTTP、自动重连、能过代理。
而且断线重连的语义很清晰：**先订阅、再取快照**，中间的增量由 Redis Pub/Sub 补齐
（先取快照再订阅会丢掉两者之间的窗口，这个 gap 是真实踩出来的）。

### 4.4 幂等怎么保证

| 层 | 手段 |
|---|---|
| 数据层 | `UNIQUE (task_id, iteration)` —— 重复写同一步直接失败，不会产生两条 |
| 消费层 | worker 取到消息后**先查状态**，不是 PENDING 就不跑（不是"消息来了就跑"） |
| 调度层 | "最新一次尝试说了算"（按 `rowid`/`seq` 排序）—— 重试成功过的节点不会被旧的 FAILED 行拖死 |

### 4.5 节点级重试为什么这么难写对

一个节点重试会**新建一条 task 行**，于是同一个 `node_id` 可能同时存在
`FAILED`（第 1 次）和 `COMPLETED`（第 2 次）。如果按"扫描所有行、见到 FAILED 就记一笔"，
调度器会把**已经跑成功**的 workflow 判成失败。

所以：排序键必须严格等于插入顺序（SQLite 用 `rowid`，PG 用显式 `seq` 列），
分类时每个分支都要把另外两个集合清掉。

---

## 5. 可观测性（三根支柱都落地了）

```jsonc
// Metrics：花了多少钱
GET /workflows/{id}/cost
{"input_tokens": 2992, "output_tokens": 816, "cost_usd": 0.001705, "budget_usd": 0.05}

// Tracing：时间花在哪一步
GET /workflows/{id}/trace
{"critical_path": ["beijing_pop", "compare_summary"], "critical_path_ms": 12427,
 "slowest_node": {"node_id": "shanghai_pop", "duration_ms": 7270, "llm_ms": 7175},
 "most_tokens_node": {"node_id": "beijing_pop", "input_tokens": 3459}}

// Logging：发生了什么（一行一个 JSON，grep 即用）
{"ts":"2026-10-01T08:15:30.572Z","level":"INFO","event":"step_saved",
 "logger":"app.agent_fc_persistent","task_id":"t-1","workflow_id":"wf-1",
 "iteration":1,"input_tokens":607,"output_tokens":88}
```

**`duration_ms` 和 `llm_ms` 分开报**：一个节点慢，可能是模型慢，也可能是工具慢。
只看一个数字永远说不清该优化哪里（实测某节点 7430ms 里 7363ms 是模型延迟）。

**上下文用 `contextvars` 而不是全局变量**：入口绑一次 `task_id`，
下面十几层函数的日志自动带上。全局 dict 在并发时会串号 ——
测试 `test_context_is_isolated_between_concurrent_tasks` 用 `asyncio.gather` +
`await asyncio.sleep(0)` 制造真实交错，把它变成可执行证明。

---

## 5.1 演示与故障注入（可复现证据）

三条素材都是真跑出来的（真实容器栈 + 真实模型调用），脚本在 `scripts/demo/`，产物在 `docs/demo/`：

| 素材 | 内容 | 复现 |
| --- | --- | --- |
| [`docs/demo/cost-trace.md`](docs/demo/cost-trace.md) | 一条 workflow 的 `/cost` 与 `/trace` 原始响应：6382 入 + 3478 出 token、$0.005393（预算 $0.05）；关键路径 12.9 s；最慢节点 7.5 s 里 7.4 s 是模型延迟 | `python3 scripts/demo/capture_cost_trace.py` |
| [`docs/demo/loadtest.md`](docs/demo/loadtest.md) | 6 条 workflow 并发：1 worker 119.8 s（3.0 条/分钟）→ 3 worker 45.7 s（7.9 条/分钟），吞吐 2.6×，成本几乎不变（$0.0318 → $0.0307） | `python3 scripts/demo/loadtest.py --label worker-1` |
| [`docs/demo/fault-injection.md`](docs/demo/fault-injection.md) | 故障注入三连：kill worker（63.6 s 回收 + 节点重试成功）、kill Redis（4 个 PENDING 搁浅 → 补偿重投 39.4 s 自愈）、极小预算（终态 FAILED、不重试） | `python3 scripts/demo/fault_injection.py --scenario all` |

> 这些脚本会花真实 API 额度：一条三节点 workflow 约 $0.005。

## 6. 数据库：SQLite 与 PostgreSQL 双方言

| 差异 | SQLite | PostgreSQL | 抹平方式 |
|---|---|---|---|
| 占位符 | `?` | `%s` | `_translate()` |
| 当前时间 | `datetime('now')` | `to_char(...)` | `_translate()` |
| 自增序号 | 隐式 `rowid` | 显式 `seq` 列 | `_translate()` 改写 |
| 迁移 DDL | `PRAGMA table_info` | `ADD COLUMN IF NOT EXISTS` | `ensure_schema()` 分方言 |

**业务 SQL 一行都不用改**，差异全部收在 `app/db.py` 里。
`DATABASE_URL` 设了就用 PG，否则用 SQLite 文件。

**为什么保留 SQLite**：238 条测试跑在临时 SQLite 上只要 1 秒、零外部依赖。
"能换库"不等于"必须换库" —— 测试用 SQLite、生产用 PG 是最划算的组合。

---

## 7. 踩过的坑（每个都有回归测试）

| 坑 | 症状 | 教训 |
|---|---|---|
| 并行工具调用 | 一次响应返回 2-6 个 tool_call，第 2 条撞 UNIQUE → worker 崩 | 假对象只造 1 个 tool_call，测试覆盖不到 |
| 加列漏读路径 | 加 `budget_usd` 列后端点 KeyError 500 | 加列只是第一步，**所有读路径**都要跟上 |
| 加状态漏快照 | 新状态没进某个白名单 → 合法状态没人处理 | 加状态要 grep 所有 `WHERE status IN (...)` |
| **配置散落** | API 写一个库、worker 读另一个库 → 任务提交 202 后永不执行、零报错 | 配置收成一处（`app/config.py`） |
| 时间戳格式不一致 | PG 返回 `...346574+00`，与 SQLite 混用相减 → TypeError | 方言差异要**逐字符**对齐 |
| 容器里读不了代码 | `PermissionError: /app/app/db.py` | `COPY` 的权限位来自构建上下文，要 `--chown` |
| 启动快照是空的 | 先打日志、后配日志 → 第一条进了 lastResort | 顺序：**先配置日志，再打日志** |
| **Redis 丢消息** | kill Redis 后，队列里的 task_id 全没了；任务还是 PENDING、node 又已在 dispatched 里 → 调度器不再派发，流水线永久停在 RUNNING（故障注入实测） | 队列只是投递层、状态才是事实来源：按 DB 补偿重投（`ORPHAN_TASK_RECOVERY_SECONDS`，默认关） |

---

## 8. 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | 必填 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | |
| `DATABASE_URL` | — | 设了就用 PostgreSQL |
| `DATABASE_PATH` | `agent.db` | 未设 `DATABASE_URL` 时的 SQLite 文件 |
| `REDIS_URL` | `redis://localhost:6379/0` | |
| `ORCHESTRATOR_POLL_INTERVAL` | `2` | 编排轮询间隔（秒） |
| `STALE_TASK_MINUTES` | `15` | 卡死任务回收阈值（RUNNING 超过它就标记 FAILED，交给节点级重试） |
| `ORPHAN_TASK_RECOVERY_SECONDS` | `0`（关闭） | 队列消息丢失后，超过这个秒数仍 `PENDING` 的任务会被补偿重投；补偿后推进 `updated_at`，避免每个 tick 重复投递 |
| `LOG_PATH` | 空（只写 stdout） | 设了同时写文件 |

配置由 `app/config.py` 统一定义，**类型错误在启动时就会失败**，不会拖到运行中。

---

## 9. 已知限制

- **Planner 那次 LLM 调用没有记账**（它不在 `steps` 表里）→ 成本报表只覆盖执行阶段。
- **`TEXT` 列尚未升级为 `TIMESTAMPTZ` / `JSONB`** —— 迁移先做到"两个方言都能跑"，
  类型升级是第二步（改动面在读写边界）。
- **编排器不可横向扩展**（有状态）。多副本需要引入分布式锁或 leader 选举。
- **没有做速率限制和鉴权** —— 这是个学习项目，不是可以直接上公网的服务。
