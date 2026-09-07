# 多 Agent 任务编排平台 — 从 0 到 1 教学指南

> **本文档用途**：这是一份"任务驱动式"教学大纲。学生（大学生，学习 Agent 后端开发）在 AI 助教的引导下完成整个项目。
> 任何接手的 AI 助教请先阅读下面的《助教指令》，然后从《当前进度》标记的位置继续教学。

---

## 📋 给 AI 助教的指令（每次会话开始先读这里）

你是一位后端教学导师，学生是大学生，目标是通过这个项目学会 Agent 后端开发并应对面试。教学规则：

1. **不要直接替学生写核心代码**。你负责：讲解概念 → 给出带 `# TODO` 的代码框架 → 布置任务 → 提出思考题 → 学生提交后检查/点评。
2. **基础设施类代码可以直接给**（如 requirements.txt、Docker 配置、样板代码），**核心逻辑必须让学生自己填**（如 Agent 循环、重试逻辑、DAG 调度）。
3. 每个任务完成后，用本文档中的《验收标准》验证，通过后才进入下一个任务。
4. 学生回答思考题后，对照本文档中的《参考答案》点评：答对了要指出好在哪，答错了不要直接给答案，先给提示引导再想一次。
5. 学生卡住超过 2 次提示后，可以给出答案并解释。
6. 每完成一个阶段，更新本文档底部的《当前进度》一节。
7. 用中文交流，代码注释用中文。
8. 项目路径：`/home/asus/projects/agent-platform/`，虚拟环境在 `venv/`（用 `source venv/bin/activate` 激活）。

---

## 🗺️ 项目总览

**目标**：用户提交复杂目标（如"调研某行业并生成报告"），系统拆解成多个子任务（DAG），分派给不同 Agent 执行，Agent 可调用工具，全程可视化进度，支持失败重试和人工审批。

**技术栈（实际采用）**：FastAPI + SQLite（阶段 3 起步，后续可换 PostgreSQL）+ Redis（阶段 4 引入）+ **DeepSeek API（通过 openai SDK 的 OpenAI 兼容接口调用）** + 手写 Agent loop（不用框架）+ Docker Compose（阶段 8）

> ⚠️ 助教注意：原计划用 Anthropic API，学生实际选择了 **DeepSeek**（模型 `deepseek-v4-flash`）。调用方式是 `openai.OpenAI(api_key=..., base_url="https://api.deepseek.com/v1")`，**不要**用 anthropic SDK 或 `client.messages.create`，要用 `client.chat.completions.create`。数据库也从 PostgreSQL 改为先用 SQLite 起步（更轻，学生本地无需装服务）。

**阶段划分**（每个阶段产出一个可运行的成果）：

| 阶段 | 主题 | 核心产出 | 状态 |
|---|---|---|---|
| 0 | 环境准备 | venv + 依赖 + 项目结构 | ✅ 已完成 |
| 1 | 手写 Agent Loop（ReAct） | `/agent/run` 接口，文本解析式循环 | ✅ 已完成 |
| 2 | Function Calling 重构 | 用原生 tools API 替代正则解析 | ✅ 已完成 |
| 3 | 状态持久化 + 重试 | 落库、断点续跑、执行回放 | 🟡 主体完成，有 2 个遗留项 |
| 4 | 异步任务 + SSE | 任务后台执行、前端实时看进度 | ⬜ 未开始 |
| 5 | DAG 编排 + 多 Agent | 规划者-执行者模式、依赖调度 | ⬜ 未开始 |
| 6 | 人工审批（HITL） | 关键步骤暂停等人批准 | ⬜ 未开始 |
| 7 | 可观测性 | token 计量、成本、结构化日志 | ⬜ 未开始 |
| 8 | Docker 部署 + 收尾 | docker-compose 一键启动、README | ⬜ 未开始 |

---

# 阶段 0：环境准备 ✅（已完成）

已完成的内容：
- [x] `python3 -m venv venv` 创建虚拟环境（曾遇到 `python3-venv` 未安装问题，已用 `sudo apt install python3-venv` 解决）
- [x] `requirements.txt`：fastapi、uvicorn、pydantic、**openai**（不是 anthropic）、python-dotenv，版本号全部用 `>=` 放宽
- [x] `.env` 已填入真实 DeepSeek key，配置项为 `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL`
- [x] API 连通性已验证通过

**当前实际文件结构**（截至阶段 3 完成）：

```
agent-platform/
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI 入口（已完成，跑通 /agent/run + /health）
│   ├── agent.py                 # 阶段1：文本解析版 loop（已完成）
│   ├── agent_fc.py              # 阶段2：Function Calling 版（已完成）
│   ├── agent_fc_persistent.py   # 阶段3：FC + 落库 + resume（主体完成）
│   └── tools.py                 # 工具函数 + TOOLS 注册表 + tools（JSON Schema 列表）
├── agent.db                     # SQLite 数据库（tasks / steps 两张表）
├── init_db.py                   # 建表脚本
├── migrate_add_llm_message.py   # 迁移：给 steps 加 llm_message_json 列
├── test_parse.py                # 阶段1：测正则解析
├── test_run.py                  # 阶段1：测文本解析版 loop
├── test_run_fc.py               # 阶段2：测 Function Calling 版
├── test_run_persistent.py       # 阶段3：测落库
├── test_resume.py               # 阶段3：测断点续跑
├── knowledge.md                 # 学生的知识点复盘（大白话版，31 节）
├── requirements.txt
├── .env                         # 含真实 key，勿提交 git
└── TEACHING_GUIDE.md            # 本文档
```

**⚠️ 注意事项**：
- Python 3.14 较新，包版本必须用 `>=` 放宽，否则 pydantic_core 会走源码编译卡死几分钟。
- **`.env` 里已是真实 key**，还没建 git 仓库。**首次 `git init` 前必须先写 `.gitignore`**（至少含 `.env`、`venv/`、`agent.db`、`__pycache__/`）。这件事还没做，助教记得提醒。
- 运行任何脚本前需 `cd /home/asus/projects/agent-platform && source venv/bin/activate`，且要在项目根目录跑（否则 `from app.xxx import` 找不到包）。

---

# 阶段 1：手写 Agent Loop（ReAct 模式）✅（已完成）

> **完成情况**：TODO 1-12 全部由学生填完，端到端跑通（`uvicorn` + `curl` 验收通过）。
> 学生独立答对的思考题：格式错误喂回去重试、assistant/user 消息顺序、工具不存在返回字符串而非抛异常。
> 助教教过的 Python 基础：形参 vs 实参、`and` 不是 `&&`、字典键要加引号、空列表取 `[-1]` 会 IndexError、`\n` vs `/n`、`.group(1)`、非贪婪 `+?`。
> **遗留项**：任务 1-F 的重复调用检测「逻辑已写对但从未真正触发验证过」——伪造轨迹的测法不可靠（参数对不上），留到有单元测试后补。

## 1.1 核心概念（先给学生讲）

**Agent = LLM + 工具调用循环**。ReAct 循环：

```
1. Thought（思考）: LLM 分析任务，决定下一步
2. Action（行动）: 选择工具并给出参数
3. Observation（观察）: 程序执行工具，把结果喂回给 LLM
4. 重复 1-3，直到 LLM 输出 Final Answer 或达到 max_iterations
```

关键认知：**LLM 本身不会执行工具**。它只是输出"我想调用 search，参数是北京"这样的文本，真正执行工具的是你的 Python 代码，然后把结果拼回对话历史再问 LLM。这个"拼回去再问"的 while 循环就是 Agent 的本质。

## 1.2 已给出的代码

- `app/tools.py` ✅ 已完成（含 calculator、search 两个 mock 工具、TOOLS 注册表、get_tool_descriptions()）
- `app/agent.py` ⏳ 框架已给出，含 TODO 1-12，学生正在做

## 1.3 任务清单

### 任务 1-A：完成 `_parse_llm_response()` 的正则解析（TODO 1-3）

学生需要补全提取 Action 和 Action Input 的正则：

```python
# 期望学生写出类似：
action_match = re.search(r"Action:\s*(.+?)(?:\n|$)", response_text)
action_input_match = re.search(r"Action Input:\s*(.+?)(?:\n|$)", response_text)
```

**思考题 1**：如果 LLM 输出里自己编造了 `Observation: 结果是14`（幻觉），你的解析和循环会出什么问题？怎么防御？
> **参考答案（助教用）**：LLM 可能"自问自答"，跳过真实工具执行导致答案基于幻觉。防御：① system prompt 明确禁止输出 Observation；② 解析时只取第一个 Action，把 Action Input 之后、遇到 "Observation" 就截断；③ 用 stop sequences（在 API 调用里设 `stop_sequences=["Observation:"]`），这是最可靠的方式。

**思考题 2**：如果 LLM 漏掉了 `Action Input:`，代码会发生什么？应该怎么处理？
> **参考答案**：`action_input_match` 为 None，走到 `type: "error"` 分支。处理策略：把错误信息作为 Observation 喂回去（"你的输出格式不对，请重新按格式输出"），让 LLM 自我修正，而不是直接崩溃。这就是"错误恢复"的雏形，重试 2-3 次仍失败才真正报错。

**验收标准**：写一个临时测试脚本（不调 LLM），用下面三段文本测 `_parse_llm_response`：
```python
# 测试1: 正常 action → 应返回 type=action, action=search, action_input=Python
# 测试2: 含 Final Answer → 应返回 type=final_answer
# 测试3: 缺 Action Input → 应返回 type=error
```

### 任务 1-B：完成 `_call_tool()`（TODO 4-5）

要求：工具不存在时返回友好错误文本（喂回给 LLM 让它换工具），工具抛异常时 catch 住返回错误文本。**核心原则：工具层的任何失败都不应让整个循环崩溃，而是变成 Observation 喂回去。**

**思考题 3**：为什么工具执行失败要"返回错误字符串"而不是"抛出异常"？
> **参考答案**：抛异常会中断循环，而返回错误字符串会作为 Observation 进入对话，LLM 看到错误后能自我修正（换参数重试、换工具）。这是 Agent 比传统流水线灵活的关键——错误也是信息。当然要配合 max_iterations 防止无限重试。

### 任务 1-C：完成 `run()` 主循环（TODO 6-12）

学生需要：
1. 真正调用 Anthropic API（TODO 6）：
```python
response = self.llm_client.messages.create(
    model="claude-sonnet-4-5",     # 用最新可用的模型名
    max_tokens=1024,
    system=system_prompt,
    messages=messages,
    stop_sequences=["Observation:"],   # 防止 LLM 幻觉自答（呼应思考题1）
)
llm_response_text = response.content[0].text
```
2. 串起解析 → 工具执行 → 记录 trajectory → 追加消息历史。
3. 错误分支：格式解析失败时把提示喂回去重试（呼应思考题 2）。

**思考题 4**：`messages.append({"role": "assistant", ...})` 和 `{"role": "user", "content": "Observation: ..."}` 这两行为什么必不可少？删掉会怎样？
> **参考答案**：LLM 是无状态的，每次调用只看到 messages 里的内容。不追加历史，LLM 下一轮就"失忆"，会重复调用同一个工具。这也引出后面的"上下文窗口管理"话题——历史越长 token 越贵，是阶段 7 成本控制的伏笔。

**思考题 5**：为什么要记录 trajectory？它在后续哪些功能会用到？
> **参考答案**：① 调试和演示（面试时展示执行过程）；② 阶段 3 的持久化和执行回放；③ 阶段 4 的实时进度推送（推的就是 trajectory 增量）；④ 阶段 7 的可观测性（每步 token/耗时挂在 trajectory 上）。trajectory 是整个项目的数据主线。

### 任务 1-D：完成 `app/main.py` FastAPI 接口

框架（可直接给学生，让学生填 TODO）：

```python
from fastapi import FastAPI
from pydantic import BaseModel
from dotenv import load_dotenv
from anthropic import Anthropic
from app.agent import AgentExecutor
import os

load_dotenv()
app = FastAPI(title="Agent Platform")

class AgentRequest(BaseModel):
    question: str
    max_iterations: int = 5

@app.post("/agent/run")
async def run_agent(request: AgentRequest):
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    executor = AgentExecutor(llm_client=client, max_iterations=request.max_iterations)
    result = executor.run(request.question)   # 注意：这是同步阻塞调用，阶段4会改造
    return result

@app.get("/health")
async def health():
    return {"status": "ok"}
```

启动命令：`uvicorn app.main:app --reload --port 8000`

**思考题 6**：`executor.run()` 是同步阻塞的，而接口函数是 `async def`。这样写有什么隐患？
> **参考答案**：async 函数里跑阻塞的同步代码会卡住整个事件循环，其他请求全被堵塞。临时方案：改成普通 `def`（FastAPI 会丢到线程池）。根本方案：阶段 4 的异步任务架构（提交后立即返回 task_id，后台执行）。这道题是阶段 4 的铺垫，学生答不上来没关系，埋个种子。

### 任务 1-E：端到端验收

```bash
curl -X POST http://localhost:8000/agent/run \
  -H "Content-Type: application/json" \
  -d '{"question": "北京有多少人口？请先搜索，然后计算人口数除以100"}'
```

**验收标准**：
- [ ] trajectory 中至少出现 2 次工具调用（search → calculator）
- [ ] 每步都有 thought / action / action_input / observation
- [ ] 最终 answer 正确（约 21 万，因为 2100万/100）
- [ ] 问一个不需要工具的问题（如"你好"），Agent 应直接 Final Answer，不乱调工具
- [ ] 把 max_iterations 设为 1，验证会返回"达到最大迭代次数"

### 任务 1-F（收尾）：防无限循环加固

给 AgentExecutor 增加**重复调用检测**：如果连续两次 action + action_input 完全相同，强制注入一条提示（"你在重复相同操作，请换思路或直接给出答案"）。

**思考题 7（面试题预演）**："如何防止 Agent 无限循环烧钱？"要求学生说出至少 3 层防线。
> **参考答案**：① max_iterations 硬上限；② 重复调用检测（相同 action+input 出现 N 次就干预/终止）；③ token/成本预算上限（阶段 7 实现：累计 input+output tokens × 单价，超预算终止）；④ 单次任务超时（阶段 4 实现）。回答时要体现"多层防御"思想。

---

# 阶段 2：用 Function Calling 重构 ✅（已完成）

> **完成情况**：`app/agent_fc.py` 已跑通，同一道题（上海人口÷10）下比阶段 1 更"听话"——真的调了 calculator，而阶段 1 会心算跳过。
> **实现细节与原计划的差异**：
> - Schema 列表在 `app/tools.py` 里的变量名是小写 `tools`（不是计划里写的 `TOOL_SCHEMAS`），导入时用 `from app.tools import TOOLS, tools as TOOL_SCHEMAS`。
> - 用的是 OpenAI 兼容格式（`{"type":"function","function":{...,"parameters":{...}}}`），不是 Anthropic 的 `input_schema` 格式。
> - 结果回传用 `role:"tool"` + `tool_call_id`（OpenAI 格式），不是 Anthropic 的 `tool_result` 内容块。
> - 学生答对的思考题：正则解析函数不再需要（返回已是结构化）、description 越详细 LLM 选工具越准。
> **未做**：任务 2-A 的进阶项「用装饰器从函数签名自动生成 schema」（目前 schema 是手写的，和函数定义有两份，存在不一致风险）。

## 2.1 核心概念

阶段 1 的正则解析脆弱（LLM 稍微改格式就崩）。生产级方案是 **Function Calling / Tool Use**：把工具的 JSON Schema 传给 API，LLM 返回结构化的 `tool_use` 块，不再需要正则。

对比讲解（这是面试高频题"自研 loop 和框架的区别"的基础）：

| | 阶段1 文本解析 | 阶段2 Function Calling |
|---|---|---|
| 格式可靠性 | 靠 prompt 约束，会跑偏 | API 层保证 JSON 合法 |
| 参数类型 | 只有字符串 | 有 schema，支持多参数/类型校验 |
| 代码量 | 正则一堆 | 解析代码几乎消失 |
| 可移植性 | 任何 LLM 都能用 | 依赖厂商 API 能力 |

## 2.2 任务清单

### 任务 2-A：给工具定义 JSON Schema

改造 `app/tools.py`，每个工具增加 schema。给学生的目标结构：

```python
TOOL_SCHEMAS = [
    {
        "name": "calculator",
        "description": "计算数学表达式，如 2+3*4",
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "数学表达式"}
            },
            "required": ["expression"],
        },
    },
    # TODO: 学生自己写 search 的 schema
]
```

进阶要求（学生自己实现）：写一个 `@tool` 装饰器或注册函数，从函数签名和 docstring **自动生成** schema，避免手写两份。这是"工具注册表"的雏形。

### 任务 2-B：重写 Agent 循环（新文件 `app/agent_fc.py`，保留阶段 1 的版本做对比）

核心 API 用法（给学生看的最小示例）：

```python
response = client.messages.create(
    model="claude-sonnet-4-5",
    max_tokens=1024,
    tools=TOOL_SCHEMAS,
    messages=messages,
)
# response.stop_reason == "tool_use" 时：
#   遍历 response.content 找 type=="tool_use" 的块 → block.name / block.input / block.id
#   执行工具后，追加：
#   messages.append({"role": "assistant", "content": response.content})
#   messages.append({"role": "user", "content": [
#       {"type": "tool_result", "tool_use_id": block.id, "content": 工具结果字符串}
#   ]})
# response.stop_reason == "end_turn" 时：LLM 给出了最终答案，取 text 块即可
```

学生要自己实现：循环骨架、多个 tool_use 块的处理、trajectory 记录（和阶段 1 字段对齐）。

**思考题 8**：`tool_use_id` 的作用是什么？如果一次响应里有两个工具调用，漏传一个 tool_result 会怎样？
> **参考答案**：id 把"调用请求"和"执行结果"配对，因为一次响应可能有多个并行工具调用。漏传会导致 API 报错（每个 tool_use 都必须有对应 tool_result）。这是并行工具调用的基础，也是消息协议设计的一个好案例。

**思考题 9**：Function Calling 模式下还需要 max_iterations 吗？
> **参考答案**：需要。Function Calling 只解决"格式可靠"，不解决"行为收敛"。LLM 仍可能反复调工具不收敛。防线一个都不能少。

**验收标准**：
- [ ] 同样的验收用例（北京人口题）在 `/agent/run?mode=fc` 下通过
- [ ] 故意让工具抛错（如 calculator 传入 `1/0`），Agent 能看到错误并自我修正
- [ ] 学生能口头对比两种模式的优劣（对照 2.1 的表格）

---

# 阶段 3：状态持久化 + 失败重试 + 断点续跑 🟡（主体完成，有遗留项）

> **⚠️ 助教必读：本阶段实际实现与下面的计划有较大偏差，且有一个已知 bug 待修。**
>
> **实际用的技术**：SQLite（不是 PostgreSQL）+ 原生 `sqlite3` 模块（**没用 SQLAlchemy**）。建表脚本 `init_db.py`，迁移脚本 `migrate_add_llm_message.py`。
>
> **已完成**：
> - [x] 任务 3-A 表设计：`tasks`（id/question/status/answer/max_iterations/created_at/updated_at）+ `steps`（id/task_id/iteration/type/tool_name/tool_args/observation/started_at/finished_at/**llm_message_json**），含 `UNIQUE(task_id, iteration)`
> - [x] 任务 3-C 每步落库：`app/agent_fc_persistent.py` 的 `_create_task` / `_update_task_status` / `_save_step`
> - [x] 任务 3-E 断点续跑 `resume()`：走的是**方案 A**（把 LLM 原始 message 用 `model_dump()` 序列化存进 `llm_message_json`，恢复时直接重建 messages）
> - [x] 学生答对的思考题：`UNIQUE` 约束防重复执行、resume 要读 steps + 重建 messages + 从下一轮继续
>
> **为什么走方案 A**：先试过"手工构造 assistant 消息"，DeepSeek 报 `400 The reasoning_content in the thinking mode must be passed back to the API`——推理模型要求原样回传 `reasoning_content`。教训：**厂商特有字段无法手工伪造，只能存原始响应**。这是个很好的教学案例，值得让学生讲给面试官。
>
> **🔴 已知 bug（下次上课第一件事修它）**：
> `agent_fc_persistent.py:263` 的条件是 `if step_type == "tool_call" and llm_message_json:`，但 `start_iteration = len(completed_steps)`（:293）把**所有** step 都算进去了。后果：`llm_message_json` 为 NULL 的旧数据行会被**静默跳过**——不进 messages、不进 trajectory，但仍占用 iteration 计数。实测表现：resume 后 search 被**重跑了一遍**（steps 表里 iteration 1 和 2 是两条一模一样的 search），返回的 trajectory 也丢了第 1 步。**也就是说 resume 该省的 token 并没省下来**，恰好是这个功能存在的意义。
> 修法建议（让学生自己选并说明理由）：① 遇到缺 `llm_message_json` 的行就明确报错，提示该任务是迁移前的旧数据无法续跑；② 或降级成"摘要注入 system prompt"并只对这些行降级；③ 或 `start_iteration` 改为只数成功重建的行数。
> 另外 `test_resume.py` 里手工 INSERT 的假 step 没有 `llm_message_json`，所以那个测试恰好一直在走这条坏路径——**测试本身也要一起改**。
>
> **未做的计划项（别当已完成）**：
> - [ ] 任务 3-B 状态机校验：`VALID_TRANSITIONS` 字典和非法转移拦截**完全没写**，目前状态是裸字符串直接 UPDATE，`COMPLETED → RUNNING` 这种非法转移不会被拒
> - [ ] 任务 3-D 失败重试 + 指数退避 + jitter：**没写**（思考题 13 也还没问过学生）
> - [ ] 任务 3-F 执行回放接口 `GET /tasks/{id}`：**没写**，目前只能用 Python 直接查库。`main.py` 里还只有阶段 1 的 `/agent/run` + `/health`，**持久化版和 resume 都还没接进 FastAPI**
> - [ ] `tasks` 表没有 `error` 列，失败信息目前塞进了 `answer` 列（语义不干净，可作为一个小重构练习）

## 3.1 核心概念

目前 Agent 状态全在内存里：进程一挂全丢。生产系统要求**每一步都落库**，这带来三个能力：
1. **审计/回放**：事后能看每一步干了什么
2. **断点续跑**：进程崩溃后从最后一步恢复，而不是从头再来（重新烧钱）
3. **幂等重试**：失败的步骤可以安全重试

**状态机设计**（本阶段最核心的知识点）：

```
Task:  PENDING → RUNNING → COMPLETED
                    ↓  ↘
                 FAILED   CANCELLED
Step:  PENDING → RUNNING → COMPLETED / FAILED
```

规则：状态只能沿箭头转移；每次转移都写数据库；非法转移（如 COMPLETED → RUNNING）必须拒绝。

## 3.2 环境准备（❌ 下面这套没有采用，仅作为后续升级参考）

> **实际采用的是 SQLite**：Python 标准库自带 `sqlite3`，**零安装、零新增依赖**，数据库就是项目根目录下的 `agent.db` 一个文件。
> 建表：`python init_db.py`。加列：`python migrate_add_llm_message.py`。
> 注意 WSL2 里**没装 `sqlite3` 命令行工具**（`sqlite3: command not found`），要查库就用 Python 的 `sqlite3` 模块，别让学生去敲 `sqlite3 agent.db`。
> 从 SQLite 换 PostgreSQL 时表结构不用改，只需改连接方式和 `datetime('now')` → `NOW()`，可作为阶段 8 的进阶练习。

以下 PostgreSQL 方案**未使用**，保留供将来升级参考：

```bash
sudo apt install postgresql postgresql-contrib
sudo service postgresql start
sudo -u postgres psql -c "CREATE USER agent WITH PASSWORD 'agent123';"
sudo -u postgres psql -c "CREATE DATABASE agent_platform OWNER agent;"
```
新增依赖：
```
sqlalchemy>=2.0
psycopg2-binary
alembic
```
`.env` 追加：`DATABASE_URL=postgresql://agent:agent123@localhost:5432/agent_platform`

## 3.3 任务清单

### 任务 3-A：设计表结构（学生先自己设计，再对照参考）

要求学生先在纸上/md 里写出表设计，回答"需要哪些表、哪些字段、为什么"，然后对照参考：

```sql
-- 参考设计（学生设计合理即可，不必一模一样）
CREATE TABLE tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    question TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    answer TEXT,
    error TEXT,
    max_iterations INT DEFAULT 5,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID REFERENCES tasks(id),
    iteration INT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    thought TEXT,
    action VARCHAR(100),
    action_input JSONB,
    observation TEXT,
    input_tokens INT DEFAULT 0,     -- 阶段7伏笔
    output_tokens INT DEFAULT 0,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    UNIQUE (task_id, iteration)      -- 幂等关键！
);
```

**思考题 10**：`UNIQUE (task_id, iteration)` 这个约束防的是什么问题？
> **参考答案**：防止重复执行同一步写入两条记录——这就是**幂等**的数据库层保障。如果重试逻辑有 bug 导致同一步执行两次，唯一约束会让第二次插入失败，暴露问题而不是静默产生脏数据。面试讲幂等设计时这是很好的落地例子。

**思考题 11**：action_input 用 JSONB 而不是 TEXT，好处是什么？
> **参考答案**：可以用 SQL 查询内部字段（如统计哪个工具被调用最多 `WHERE action_input->>'query' = ...`），可加 GIN 索引。TEXT 只能整存整取。

### 任务 3-B：用 SQLAlchemy 建模 + 状态机校验

新建 `app/models.py`（ORM 模型）和 `app/state_machine.py`。状态机的核心让学生自己写：

```python
VALID_TRANSITIONS = {
    "PENDING":  {"RUNNING", "CANCELLED"},
    "RUNNING":  {"COMPLETED", "FAILED", "CANCELLED"},
    "FAILED":   {"RUNNING"},        # 允许重试
    "COMPLETED": set(),
    "CANCELLED": set(),
}

def transition(entity, new_status: str):
    # TODO 学生实现：校验合法性，非法则抛 InvalidTransitionError
    # 合法则更新 status + updated_at
    ...
```

### 任务 3-C：改造 AgentExecutor，每步落库

要求：
1. 任务开始：插入 task 行（PENDING→RUNNING）
2. 每轮循环开始：插入 step 行（RUNNING），**先落库再调 LLM**
3. 每轮结束：更新 step（observation、tokens、finished_at、COMPLETED）
4. 最终：更新 task（answer、COMPLETED 或 FAILED）

**思考题 12**：为什么是"先插 step 再调 LLM"，而不是"LLM 调完一起写库"？
> **参考答案**：如果进程在 LLM 调用中途崩溃，"先落库"能留下 RUNNING 状态的 step 记录，恢复时知道执行到哪、哪一步没完成。"调完再写"则崩溃后无痕迹，不知道该从哪续跑。这是 write-ahead 思想（预写日志）的应用，和数据库 WAL、消息队列 at-least-once 是同一个思维模型。

### 任务 3-D：失败重试（带指数退避）

写一个通用重试装饰器/函数，用于 LLM 调用和工具调用：

```python
def retry_with_backoff(fn, max_retries=3, base_delay=1.0):
    # TODO 学生实现：
    # 第 n 次失败后 sleep base_delay * (2 ** n) + 随机抖动
    # 区分可重试错误（网络超时、429限流）和不可重试错误（401认证失败）
    ...
```

**思考题 13**：为什么要加随机抖动（jitter）？为什么 401 不该重试？
> **参考答案**：① 无抖动时，大量客户端会在同一时刻同时重试（惊群/thundering herd），把刚恢复的服务再打挂；② 401 是确定性错误，重试 100 次也是 401，白白浪费时间，还可能触发风控。可重试 = 瞬时性错误（超时/429/5xx），不可重试 = 确定性错误（4xx 里的多数）。

### 任务 3-E：断点续跑

实现 `POST /tasks/{task_id}/resume`：
1. 读取该 task 已 COMPLETED 的 steps，**重建 messages 对话历史**（不重新调 LLM！）
2. 从失败/中断的那一步继续循环
3. 用一个真实场景验收：把 search 工具改成第 2 次调用必抛错 → 任务 FAILED → 修好工具 → resume → 从第 2 步继续，第 1 步不重新执行

**思考题 14**：断点续跑省的是什么？重建 messages 时要注意什么？
> **参考答案**：省的是已完成步骤的 LLM token 费用和时间。注意：messages 重建必须和原始执行时的结构完全一致（assistant 的 tool_use 和 user 的 tool_result 严格配对），否则 API 会报错或 LLM 行为异常。这就是 trajectory 表设计要存够信息的原因。

### 任务 3-F：执行回放接口

`GET /tasks/{task_id}` 返回任务详情 + 全部 steps（按 iteration 排序）。这就是"执行回放"——阶段 4 前端可视化直接吃这个接口。

**验收标准（整个阶段 3）**：
- [ ] 跑一个任务，psql 里能查到 task + steps 记录，状态全部正确
- [ ] 非法状态转移（手动把 COMPLETED 改 RUNNING）被拒绝
- [ ] kill -9 杀掉执行到一半的进程 → resume 后正确续跑，不重复执行已完成步骤
- [ ] 学生能画出状态机图并解释每条边

---

# 阶段 4：异步长任务 + SSE 实时推送

## 4.1 核心概念

回收阶段 1 的思考题 6：Agent 任务耗时几十秒到几分钟，HTTP 同步等待不现实（超时、占连接、无法展示进度）。目标架构：

```
POST /tasks         → 立即返回 {task_id}，任务入队
[后台 worker]       → 从 Redis 队列取任务，跑 Agent 循环，每步发进度事件
GET /tasks/{id}/events (SSE) → 前端订阅，实时收到每一步
```

**SSE vs WebSocket 选型**（面试送分题）：进度推送是单向的（服务器→客户端），SSE 更简单：纯 HTTP、自动重连、无需握手协议。WebSocket 适合双向（聊天、协作编辑）。本项目选 SSE。

## 4.2 环境准备（助教直接给）

```bash
sudo apt install redis-server && sudo service redis-server start
```
requirements.txt 追加：`redis`、`sse-starlette`

## 4.3 任务清单

### 任务 4-A：任务队列（Redis List 起步）

先用最简单的 `LPUSH` / `BRPOP` 实现，能跑通再讨论升级：

```python
# app/queue.py — 学生实现
def enqueue_task(task_id: str): ...          # LPUSH agent:tasks
def dequeue_task(timeout=5) -> str | None:   # BRPOP agent:tasks
```

### 任务 4-B：Worker 进程

新建 `app/worker.py`，独立进程运行（`python -m app.worker`）：

```python
# 伪代码骨架
while running:
    task_id = dequeue_task()
    if not task_id: continue
    task = load_task_from_db(task_id)
    if task.status != "PENDING": continue    # 幂等检查！
    run_agent_with_persistence(task)          # 阶段3的执行器
```

**思考题 15**：worker 取到任务后、执行前，为什么要再查一次数据库状态？
> **参考答案**：消息可能重复投递（at-least-once），任务可能已被取消/已被其他 worker 执行。以数据库状态为准做幂等检查是"队列消息只是通知，数据库才是事实来源（source of truth）"的设计原则。

**思考题 16**：如果起两个 worker 进程，同一个任务会被执行两次吗？怎么防？
> **参考答案**：BRPOP 是原子的，一条消息只会被一个 worker 弹出，基本安全。但若消息因故重复入队，就需要分布式锁：`SET lock:task:{id} {worker_id} NX EX 300`，抢到锁才执行。让学生实际写这个锁，理解 NX（不存在才设置）和 EX（过期时间防死锁）各自的作用。这就是"分布式锁"知识点的落地。

### 任务 4-C：进度事件发布（Redis Pub/Sub）

Agent 每完成一步，向 `task:{id}:events` 频道 publish 一条 JSON 事件：

```python
{"type": "step_completed", "iteration": 2, "action": "search", "observation": "...", "ts": "..."}
{"type": "task_completed", "answer": "...", "total_iterations": 3}
```

实现方式：给 AgentExecutor 加一个 `on_step` 回调参数（不要把 Redis 硬编码进执行器——讲解**依赖注入/解耦**）。

### 任务 4-D：SSE 接口

```python
from sse_starlette.sse import EventSourceResponse

@app.get("/tasks/{task_id}/events")
async def task_events(task_id: str):
    async def gen():
        # TODO 学生实现：
        # 1. 先把数据库里已有的 steps 发一遍（补历史，处理"订阅晚了"）
        # 2. 再订阅 Redis 频道，持续 yield 新事件
        # 3. 任务终态后发 task_completed 并结束
        ...
    return EventSourceResponse(gen())
```

**思考题 17**：为什么要"先发数据库历史，再订阅实时"？只订阅 Pub/Sub 会丢什么？
> **参考答案**：Redis Pub/Sub 不保存历史，订阅前发布的消息永远收不到。用户中途刷新页面就会看到空白进度。先补历史再订阅（并处理两者衔接处的去重）是实时系统的经典模式。进阶话题：Redis Streams 自带历史和 consumer group，可作为 Pub/Sub 的升级选项，让学生对比。

### 任务 4-E：任务取消 + 超时

- `POST /tasks/{id}/cancel`：数据库状态置 CANCELLED；worker 每轮循环开始前检查状态，发现 CANCELLED 就停。
- 超时：任务 RUNNING 超过 N 分钟，worker 主动终止并置 FAILED（超时也是防烧钱的一道防线）。

**思考题 18**：为什么取消是"打标记 + worker 自查"，而不是直接 kill worker？
> **参考答案**：kill 会连累同 worker 上的其他任务，且当前步骤状态来不及落库（留下永久 RUNNING 的僵尸 step）。协作式取消（cooperative cancellation）让 worker 在安全点（每轮循环边界）优雅退出，状态一致。同理引申"优雅关闭"：进程收到 SIGTERM 时应跑完当前步骤、落库后再退出，学生可顺手实现 signal handler。

### 任务 4-F：极简前端（可选但推荐）

一个纯 HTML 页面（`static/index.html`）：输入框提交任务 → `EventSource` 订阅 → 逐条渲染步骤。不用框架，50 行以内即可。

**验收标准（整个阶段 4）**：
- [ ] `POST /tasks` 100ms 内返回 task_id
- [ ] `curl -N http://localhost:8000/tasks/{id}/events` 能看到事件逐条到达
- [ ] 中途重新 curl（模拟刷新），历史步骤能补全
- [ ] 取消接口生效，worker 在下一轮循环停止
- [ ] 起 2 个 worker，提交 3 个任务，无重复执行

---

# 阶段 5：DAG 编排 + 多 Agent 协作（项目核心亮点）

## 5.1 核心概念

前面是"单任务单 Agent"。现在升级为：**用户给一个复杂目标 → 规划者 Agent 拆解成子任务 DAG → 调度器按依赖顺序分发 → 执行者 Agent 各自完成 → 汇总**。

```
用户目标："调研新能源汽车行业并生成报告"
              │
        [Planner Agent] ──拆解──►  DAG:
                                   collect_policy ──┐
                                   collect_market ──┼──► analyze ──► write_report
                                   collect_tech  ───┘
              │
        [Scheduler] 按依赖调度，无依赖的并行执行
              │
        [Executor Agents] 每个子任务 = 一次完整的 Agent 循环（复用阶段1-4全部成果）
```

**关键设计**：DAG 的每个节点不是一个函数，而是一个**完整的 Agent 任务**（阶段 3 的 tasks 表复用为子任务）。

## 5.2 任务清单

### 任务 5-A：DAG 数据结构 + 拓扑排序（纯算法，不涉及 LLM）

先写纯 Python 的 DAG 类并配单元测试（引入 pytest，这也是补测试习惯的时机）：

```python
class DAG:
    def add_node(self, node_id, payload): ...
    def add_edge(self, from_id, to_id): ...
    def validate(self): ...          # TODO: 检测环（DFS 或 Kahn 算法），有环抛异常
    def ready_nodes(self, completed: set) -> list: ...
                                     # TODO: 返回"依赖全部完成且自身未执行"的节点
```

**思考题 19**：为什么必须检测环？调度器遇到环会发生什么？
> **参考答案**：有环则互相等待，`ready_nodes` 永远返回空，调度器死锁——任务卡在 RUNNING 永不结束。必须在**接受任务时**就校验拒绝，而不是运行时才发现。快速失败（fail fast）原则。

**思考题 20**：`ready_nodes` 为什么返回列表而不是单个节点？
> **参考答案**：无依赖关系的节点可以**并行执行**（如三个 collect 任务同时跑），这是 DAG 相比线性流水线的核心价值。返回列表让调度器一次派发多个，配合阶段 4 的多 worker 实现真并行。

### 任务 5-B：表结构扩展

```sql
CREATE TABLE workflows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    goal TEXT NOT NULL,                  -- 用户的原始目标
    status VARCHAR(20) DEFAULT 'PLANNING',
    dag JSONB,                           -- 规划出的 DAG 结构
    final_output TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
-- tasks 表加列：
ALTER TABLE tasks ADD COLUMN workflow_id UUID REFERENCES workflows(id);
ALTER TABLE tasks ADD COLUMN node_id VARCHAR(100);       -- 在 DAG 中的节点名
ALTER TABLE tasks ADD COLUMN depends_on JSONB DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN upstream_outputs JSONB;     -- 上游结果注入
```

### 任务 5-C：Planner Agent（规划者）

Planner 是一次特殊的 LLM 调用：输入用户目标，输出 JSON 格式的 DAG。让学生设计 prompt，要求输出：

```json
{
  "nodes": [
    {"id": "collect_market", "description": "搜索并整理市场规模数据", "agent_type": "researcher"},
    {"id": "analyze", "description": "综合分析上游数据", "agent_type": "analyst", "depends_on": ["collect_market", "..."]}
  ]
}
```

工程要点（学生实现）：
1. 用 Function Calling 强制 JSON 输出（定义一个 `submit_plan` 工具，schema 即 DAG 结构——比"请输出JSON"提示词可靠得多）
2. 输出后立即 `DAG.validate()`，不合法（有环/引用不存在的节点）就把错误喂回去让 Planner 重新规划（最多 3 次）
3. 限制节点数上限（如 ≤10），防止 Planner 拆出 100 个子任务烧钱

**思考题 21**：为什么用 Function Calling 拿 JSON 比提示词"请只输出JSON"可靠？
> **参考答案**：提示词方式 LLM 常夹带解释文字、markdown 代码块包裹、字段拼错；Function Calling 由 API 层面保证返回合法 JSON 且符合 schema。"让协议层保证格式，让提示词专注内容"是工程化的关键一步。

### 任务 5-D：Workflow 调度器

核心循环（学生实现，这是整个项目最有含金量的代码）：

```
loop:
  completed = 查询该 workflow 下 COMPLETED 的节点
  if 全部完成: 汇总 → workflow COMPLETED, break
  if 有节点 FAILED 且不可重试: workflow FAILED, break
  ready = dag.ready_nodes(completed)
  for node in ready(且未入队):
      收集上游节点的输出 → 写入该子任务的 upstream_outputs
      创建 task 行 + enqueue(阶段4队列)
  sleep / 或事件驱动等待
```

工程要点：
- **上下文传递**：下游 Agent 的 prompt = 节点 description + 上游输出摘要。上游输出太长怎么办？→ 截断或让上游 Agent 先自我总结（上下文窗口管理知识点落地）
- 调度器本身也要防重复入队（内存 set 或数据库状态判断）

**思考题 22**：调度器是"轮询数据库"还是"订阅任务完成事件"？两种方式的取舍？
> **参考答案**：轮询实现简单、不依赖消息可靠性，但有延迟且空转浪费；事件驱动实时性好，但要处理事件丢失（订阅断线期间完成的任务）。生产常用"事件驱动为主 + 低频轮询兜底"。学生先实现轮询版跑通，有余力再加事件触发。

### 任务 5-E：执行者分化（多 Agent 协作）

按 `agent_type` 给执行者不同的 system prompt 和工具集：

```python
AGENT_PROFILES = {
    "researcher": {"system": "你是调研员，擅长搜集信息...", "tools": ["search"]},
    "analyst":    {"system": "你是分析师...",             "tools": ["calculator", "search"]},
    "writer":     {"system": "你是报告撰写者...",          "tools": []},
}
```

**思考题 23**：为什么不给所有 Agent 全部工具？
> **参考答案**：① 工具越多，LLM 选错工具概率越高（选择过载）；② 角色专注度：writer 有 search 就可能忍不住去搜新资料，偏离"基于上游结果写作"的职责；③ 权限最小化，与安全原则一致；④ 工具 schema 也占 token，白花钱。

### 任务 5-F：条件分支（进阶，可选）

节点增加 `condition` 字段（如"仅当 analyze 的输出包含风险提示时执行 deep_risk_review"）。实现：上游完成后，用一次轻量 LLM 调用判断条件真假，false 则把节点标记 SKIPPED（SKIPPED 视为"已完成"参与依赖计算）。

**验收标准（整个阶段 5）**：
- [ ] DAG 类单元测试通过（含环检测、菱形依赖 ready_nodes 正确性）
- [ ] 提交"调研XX并生成报告"，能看到：规划出的 DAG → 三个 collect 并行执行 → analyze 等齐依赖 → report 汇总
- [ ] 一个子任务失败 → 重试 → 仍失败 → workflow FAILED，但其他分支已完成的结果保留
- [ ] SSE 前端能看到 workflow 级 + 子任务级两层进度
- [ ] 学生能讲清 planner-executor 模式：为什么拆两阶段而不是一个巨型 Agent 从头干到尾（答案要点：单 Agent 长任务上下文爆炸、无法并行、错误传播不可控、无法局部重试）

---

# 阶段 6：人工审批（Human-in-the-Loop）

## 6.1 核心概念

有些步骤不能全自动（发邮件、花钱、删数据、对外发布报告）。需要：执行到关键节点时**暂停**，通知人类，等待批准/拒绝，然后**恢复**。

技术本质 = 阶段 3 的断点续跑 + 一个新状态：

```
Task: ... → RUNNING → WAITING_APPROVAL → RUNNING（批准）
                          └────────────→ CANCELLED（拒绝）
```

## 6.2 任务清单

### 任务 6-A：审批点定义

两种触发方式都实现：
1. **静态**：DAG 节点标记 `"requires_approval": true`（Planner 的 schema 里加此字段，让 LLM 自己判断哪些步骤危险 + 允许用户在提交时指定）
2. **动态**：给 Agent 一个特殊工具 `request_human_approval(reason, proposed_action)`，Agent 自己意识到"这步需要人确认"时调用

### 任务 6-B：暂停与恢复机制

学生实现：
1. 触发审批 → task 置 WAITING_APPROVAL，**保存当前完整上下文**（messages 快照，复用阶段3的能力）→ worker 释放该任务（不占着 worker 干等！）
2. `GET /approvals/pending` 列出待审批项（展示：哪个任务、哪一步、Agent 想干什么、理由）
3. `POST /approvals/{id}/decide {"approved": true/false, "comment": "..."}` → 批准：任务重新入队，恢复时把"人类已批准，意见：xxx"作为 tool_result 注入；拒绝：注入拒绝理由，让 Agent 换方案或终止

**思考题 24**：为什么审批等待时必须释放 worker，而不是 sleep 等结果？
> **参考答案**：人类响应以小时/天计，worker 干等会被全部占满（假设 4 个 worker、5 个任务在等审批 = 系统瘫痪）。正确模型：等待状态只存在于数据库，不占任何计算资源，审批动作本身触发重新入队。这是"长时间等待要出让资源"的通用异步设计原则，Temporal 的 durable execution 就是这个思想的极致。

**思考题 25**：审批决定（tool_result 注入"已批准"）和直接改数据库状态，为什么前者更好？
> **参考答案**：注入对话让 Agent **知道**审批结果和人类意见，能据此调整后续行为（人类说"批准但金额改成100"，Agent 能理解）。只改状态则 Agent 失忆，不知道刚才发生过审批。人机协作的信息要进入 Agent 的上下文，而不是只存在于系统侧。

**验收标准**：
- [ ] 演示场景：报告生成前需人审 → 任务暂停 → 前端/接口看到待审批 → 批准后继续并在最终输出里体现人类意见 → 拒绝则优雅终止
- [ ] 等待审批期间 worker 能正常处理其他任务
- [ ] 审批记录落库（谁、何时、决定、意见）——审计需求

---

# 阶段 7：可观测性（token / 成本 / 日志 / 追踪）

## 7.1 核心概念

三大支柱在本项目的落地：
- **Metrics**：每步 token 数、耗时、成本；任务级汇总
- **Logging**：结构化日志（JSON），带 task_id/workflow_id 关联
- **Tracing**：一个 workflow 从提交到完成的完整调用链

## 7.2 任务清单

### 任务 7-A：token 计量与成本核算

API 响应里有 `response.usage.input_tokens / output_tokens`（阶段 3 表里已预留字段）。学生实现：
1. 每步记录 tokens，任务/workflow 级 SUM 汇总接口 `GET /workflows/{id}/cost`
2. 价格表配置化（`config.py`：每模型的 input/output 单价），计算成本
3. **预算上限**：workflow 创建时可设 `budget_usd`，每步执行前检查累计成本，超预算 → 任务终止并标记 BUDGET_EXCEEDED（回收思考题 7 的最后一层防线）

**思考题 26**：为什么 input_tokens 通常远大于 output_tokens？随着循环轮数增加，input_tokens 的增长是线性的吗？
> **参考答案**：每轮都要重发全部历史（system + 所有轮的对话），所以第 n 轮 input ≈ 前 n-1 轮内容总和 → 总 input 成本约 O(n²) 增长。这是"长循环烧钱"的数学本质，也是上下文窗口管理（截断/摘要历史）和 prompt caching 的动机。能讲出 O(n²) 这一点在面试里非常加分。

### 任务 7-B：结构化日志

引入 `structlog`（或标准 logging + JSON formatter）。要求：
- 所有日志 JSON 格式，统一带 `task_id`、`workflow_id`、`iteration` 字段
- 用 contextvars 自动注入上下文（不要每条日志手动传）
- 关键事件必须有日志：任务入队/开始/每步/重试/失败/审批/完成

### 任务 7-C：简易追踪

不引入 OpenTelemetry 全家桶（对学习项目太重），自己实现最小 trace：
- workflow_id 即 trace_id，每个 task/step 记录 started_at/finished_at
- `GET /workflows/{id}/trace` 返回时间线数据；前端画甘特图（可选，纯 div 也能画）
- 有余力的学生可以对接 OpenTelemetry + Jaeger（面试可提"了解并对比过"）

**验收标准**：
- [ ] 跑完一个 workflow，能回答：总花费多少钱？哪一步最慢？哪一步 token 最多？
- [ ] 设一个很低的 budget（如 $0.01），验证超预算终止
- [ ] `grep task_id 日志文件` 能串出该任务完整执行史

---

# 阶段 8：Docker 部署 + 项目收尾

## 8.1 任务清单

### 任务 8-A：Docker Compose

四个服务：`api`（FastAPI）、`worker`（可 `--scale worker=3`）、`postgres`、`redis`。骨架可以直接给，让学生填环境变量和依赖关系：

```yaml
services:
  api:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    ports: ["8000:8000"]
    depends_on: [postgres, redis]
    env_file: .env.docker
  worker:
    build: .
    command: python -m app.worker
    depends_on: [postgres, redis]
    env_file: .env.docker
  postgres:
    image: postgres:16
    environment: {POSTGRES_USER: agent, POSTGRES_PASSWORD: agent123, POSTGRES_DB: agent_platform}
    volumes: [pgdata:/var/lib/postgresql/data]
  redis:
    image: redis:7
volumes: {pgdata: {}}
```

坑点提示（让学生自己踩再解决）：容器里连数据库不能用 `localhost`（要用服务名 `postgres`）；worker 要处理 SIGTERM 优雅关闭（`docker stop` 默认 10 秒后 SIGKILL——回收阶段 4 的优雅关闭）。

### 任务 8-B：配置管理

用 `pydantic-settings` 统一配置类：所有配置从环境变量读，本地/`docker` 两套 `.env`。删掉代码里所有散落的 `os.getenv`。

### 任务 8-C：README + 面试准备

README 必须包含：架构图（mermaid）、快速启动、API 文档、设计决策（为什么 SSE 不是 WebSocket、为什么手写 loop、幂等如何保证）。

**最终面试问答自测清单**（学生每题都要能脱稿讲 2 分钟）：
1. 自研 Agent loop 和 LangGraph 的区别？（答案素材：阶段1-2 你写的每一行；LangGraph 的节点/边/checkpoint 对应你的 step/转移/持久化）
2. 任务失败怎么办？（阶段3：分类重试+退避 → 断点续跑 → 阶段6：升级人工）
3. 如何防止 Agent 无限循环烧钱？（max_iterations / 重复检测 / 预算上限 / 超时，四层）
4. 幂等怎么保证的？（唯一约束 + 消费前状态检查 + 分布式锁，三处落地）
5. 为什么规划者-执行者分离？（上下文隔离、并行、局部重试）
6. 上下文窗口怎么管理？（O(n²) 问题、上游输出摘要、历史截断）
7. 如果 QPS 涨 100 倍，架构哪里先崩？怎么扩？（开放题：worker 水平扩、Pub/Sub 换 Streams、数据库连接池/读写分离）

---

# 📈 当前进度（助教每次更新这里）

**最后更新**：2026-09-05

- [x] **阶段 0 完成**：venv、requirements.txt（openai SDK）、目录结构、`.env` 已填真实 DeepSeek key 并验证连通
- [x] **阶段 1 完成**：`app/agent.py` 文本解析版 loop，TODO 1-12 全部由学生填完，`uvicorn` + `curl` 端到端验收通过
  - 遗留：任务 1-F 重复调用检测逻辑写对了但**没真正触发验证过**
- [x] **阶段 2 完成**：`app/agent_fc.py` Function Calling 版跑通，比阶段 1 更听话（真调 calculator）
  - 未做：从函数签名自动生成 schema 的装饰器（schema 目前手写，与函数定义有两份）
- [🟡] **阶段 3 主体完成**：`app/agent_fc_persistent.py` + SQLite `agent.db`，落库和 resume 都跑通了
  - [x] **resume 历史数据 bug 已修复**：缺少 `llm_message_json` 的旧数据不再静默跳过，而是明确拒绝续跑；测试已验证不会新增重复 step。阶段 3 开头的红字说明保留作为历史案例。
  - [x] **阶段 3-G resume 修复练习**：缺少 `llm_message_json` 的旧数据会明确报错且不会新增 step；已补充自动断言，并清理完成任务的 TODO 注释。
  - [x] **阶段 3-F 执行回放接口**：`GET /tasks/{task_id}` 已实现并验证；支持任务详情、步骤回放、iteration 排序、`tool_args` JSON 还原和不存在任务的 404。
  - [x] **阶段 3-B 状态机校验**：`app/state_machine.py` 的合法/非法转移校验已实现，3 个基础测试通过，并已清理完成任务的 TODO 注释。
  - [x] **阶段 3-D 失败重试+指数退避**：可重试错误分类、指数退避、jitter、最大重试次数和负参数校验均已实现并通过 5 个测试；已清理完成任务的 TODO 注释。
- [ ] 阶段 4-8：未开始

## 🎯 下次上课建议的顺序

1. 检查 `.gitignore`，在确认 `.git` 可写后再 `git init`
2. 完成 3-D 重试+指数退避，并接入持久化执行器
3. 再进入阶段 4（异步+SSE）或整理面试材料

## 学生情况备注

- 大学生，目标是学 Agent 后端 + 面试；环境 WSL2 Ubuntu + Python 3.14
- **Python 基础较薄**，需要边做边补：已教过形参vs实参、`and`不是`&&`、字典键要加引号、空列表取`[-1]`会IndexError、`\n`vs`/n`、`**dict`解包、类型注解只是说明
- **正则表达式零基础**，阶段 1 专门补过一课（`\s*` `.+?` `(?:...)` `group(1)` 非贪婪）
- 学习方式：喜欢"你直接写完整版然后对比讲解"，也能独立答对思考题（错误喂回重试、消息顺序、工具失败返回字符串不抛异常、UNIQUE 防重复）
- 代码练习偏好：助教先在源码的准确位置加入 `TODO`；讲解代码时标注文件名和当前行号；最好同时放置注释版参考答案，学生可先独立完成，卡住后再对照。
- **能自己读懂 traceback 定位报错**，这点很好，助教应继续让他自己先看报错再给提示
- 明确要求过：写 `knowledge.md` 用大白话记录知识点（已有 31 节，阶段 1-3 全覆盖）

### 2026-09-05 课堂记录：阶段 3-G

- 思考题 1：学生答出缺失消息会导致数据库、messages、iteration 三者无法对应，判断正确。
- 思考题 2：学生用 iteration 为 `1、2、4` 的例子说明 `len(completed_steps)` 不能代表恢复起点，判断正确。
- 思考题 3：学生答出可以节省时间和 token；更准确的工程表述是：避免重复执行已完成的 LLM/工具步骤，节省 API token、费用与执行时间，并保持完整上下文。
- 核心代码验收：`restored_iterations`、缺失原始消息时拒绝续跑、按已恢复 iteration 计算起点均实现正确；语法导入通过。
- 测试验收：已补充 success、错误信息和 step 数量断言；阶段 3-G 完成。
- 阶段 3-F 验收：`GET /tasks/{task_id}` 返回结构正确；不存在任务返回 404；已清理该阶段完成后的 TODO 注释。
- 已补充 `.gitignore`，忽略 `.env`、`venv/`、`agent.db`、Python 缓存和日志；Git 初始化待确认 `.git` 目录权限后进行。
- 阶段 3-B 开课：状态机核心规则为“只允许沿合法边转移”；已提供纯 Python TODO 框架和 3 个基础测试。
- 阶段 3-D 开课：新增可重试错误分类、指数退避、jitter 和最大重试次数测试框架；验收通过后自动进入下一阶段。
- 阶段 3-D 初次验收：4 个核心行为测试通过；发现 `max_retries < 0` 和 `base_delay < 0` 尚未校验，暂不结课。
- 阶段 3-D 最终验收：5 个测试全部通过；已清理阶段 3-D 源码和测试中的 TODO/参考答案注释。

### 2026-09-07 课堂记录：阶段 3-B 验收

- `transition()` 已完成：未知状态或非法目标状态抛出 `InvalidTransitionError`；合法转移更新 `status` 和 UTC `updated_at`。
- `test_state_machine.py` 的合法转移、非法转移、失败重试三个测试全部通过。
- 已清理阶段 3-B 源码中的 TODO、参考答案和不可达的 `NotImplementedError`。

### 2026-09-07 课堂记录：阶段 3-B 思考题

- 思考题 1：学生指出 `FAILED -> RUNNING` 与 `COMPLETED -> RUNNING` 是否合理，既符合 `VALID_TRANSITIONS` 的代码定义，也符合失败后重试、完成后不应回退的业务常识。
- 思考题 2：学生指出统一调用 `transition()` 可以避免各处直接修改 `status`，集中保证业务逻辑安全可控，回答正确。
- 思考题 3：学生选择先校验再更新，并指出先更新再校验会引入回滚和二次写库问题；补充知识：生产实现通常把校验与 UPDATE 放在同一数据库事务中。

---

# 附录 A：常见环境问题速查

以下带 ✅ 的是本项目**真实踩过并解决**的，其余是预防性提示。

| 问题 | 解决 |
|---|---|
| ✅ `ensurepip is not available` | `sudo apt install python3-venv`（需在学生自己终端输密码），然后 `rm -rf venv && python3 -m venv venv` |
| ✅ pydantic_core 编译卡死几分钟 | Python 3.14 没有预编译 wheel，会走源码编 Rust。**解法：requirements.txt 全用 `>=` 放宽版本**，别锁 `==` |
| ✅ `load_dotenv()` 抛 AssertionError | 在 `python - <<EOF` 这类无文件环境里 `find_dotenv()` 定位不了。**解法：显式传路径** `load_dotenv("/绝对/路径/.env")` |
| ✅ `venv/bin/activate: No such file` | Bash 工具的工作目录会漂。**解法：每条命令都 `cd /home/asus/projects/agent-platform && source venv/bin/activate && ...`** |
| ✅ `sqlite3: command not found` | WSL2 没装 CLI 工具。**解法：用 Python 的 `sqlite3` 模块查库**，别敲 `sqlite3 agent.db` |
| ✅ DeepSeek 报 `reasoning_content must be passed back` | 推理模型要求原样回传 `reasoning_content`，手工构造的 assistant 消息会被拒。**解法：存 LLM 原始 message（`model_dump()`）再恢复**，即阶段 3 的方案 A |
| ✅ `SyntaxError: expected 'except' or 'finally'` | `try:` 块少了 `except`。改代码时容易漏，**改完先 `python -c "import app.xxx"` 验语法** |
| ✅ 模块导入失败 `No module named app` | 必须在**项目根目录**运行脚本，`app` 是根目录下的包 |
| API 调用 401 | 检查 `.env` 的 key、`load_dotenv()` 是否在读取之前执行 |
| psycopg2 装不上（将来换 PG 时） | 先 `sudo apt install libpq-dev python3-dev`，或改用 `psycopg[binary]` |
| WSL2 里服务没起（将来用 Redis/PG 时） | `sudo service redis-server start`（WSL2 默认不自动启动服务）|
| 容器连不上数据库（阶段 8） | 用 compose 服务名（`postgres`）不是 `localhost` |

# 附录 B：推荐对照学习的开源项目

| 仓库 | 学什么 | 什么时候看 |
|---|---|---|
| langchain-ai/langgraph | StateGraph、checkpoint、interrupt()（HITL） | 阶段 5-6 期间对比 |
| FoundationAgents/MetaGPT | 规划者-执行者、多 Agent 消息传递 | 阶段 5 之前通读架构 |
| temporalio/temporal | durable execution、重试/恢复模型 | 阶段 3 学理论来源 |
| hatchet-dev/hatchet | 后台任务 + agent 的编排引擎设计 | 阶段 4-5 参考 |
| n8n-io/n8n | DAG 可视化前端 | 前端参考 |
| humanlayer/12-factor-agents | 生产级 Agent 的 12 条原则 | 全程随读，面试观点来源 |
