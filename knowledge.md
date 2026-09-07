# knowledge.md — 阶段 1 知识点复盘（大白话版）

> 这份文档把阶段 1 你亲手搭起来的东西讲透。每节末尾标注它对应 agent.py / tools.py 里的哪几行，方便对照。
> 目标：让你不仅能跑通，还能跟面试官讲清"每一步为什么这么做"。

---

## 1. 什么是 Agent？一句话版本

**Agent = 会自己决定"下一步干嘛"的程序，而不是傻傻执行固定的步骤。**

普通程序是死的：输入 → 固定的处理流程 → 输出。不管输入是什么，流程不变。

Agent 是活的：它拿到问题后，**先自己分析**（"用户要查人口数"），**然后决定用什么工具**（"先搜索"），**执行完再看结果**（"拿到 500 万了"），**再想下一步**（"5000 万除以 10 = 500 万，我可以回答了"）。

这个"想一下 → 做一步 → 看结果 → 再想"的循环，就是 **Agent loop（Agent 循环）**。整份 agent.py 就是围绕这一个循环写的。

---

## 2. ReAct 模式：循环里的每一步

ReAct = **Rea**soning（思考）+ **Act**ing（行动）。它定义了循环里的四个动作：

```
1. Thought（思考）    → LLM 分析当前情况，说"我打算干嘛"
2. Action（行动）     → LLM 说"我要用哪个工具，参数是什么"
3. Observation（观察） → 程序真的去执行工具，把结果拿回来
4. 回到第 1 步，直到 LLM 说"我可以回答了"（Final Answer）
```

对应到代码里：

| ReAct 动作 | 代码里发生的事 | 位置 |
|---|---|---|
| Thought | LLM 输出一段思考文字 | 由 `_parse_llm_response` 提取 |
| Action | LLM 指定工具名 + 参数 | 同上 |
| Observation | `_call_tool()` 真正执行工具 | agent.py:163-176 |
| 循环 | `for iteration in range(...)` | agent.py:131 |
| 结束 | 解析出 `final_answer` 就 return | agent.py:148-160 |

---

## 3. 关键认知：LLM 本身不会执行工具

这是整个 Agent 最容易被误解的点。

**你以为**：LLM 是万能的，它能自己搜索、自己算数。

**实际**：LLM 只是"会说话的文本生成器"。它说"我想调用 search，参数是北京"，这句话只是一段文本。**真正去搜索、去算数的是你的 Python 代码**。

所以整个循环的本质是：

```
你（代码）             LLM
  │                     │
  │──问题──→             │
  │                     │──沉思：我要调 search──→ 你
  │←─"Action: search"── │
  │                     │
  │──执行 search(北京)→ │  ← 真正干活的是代码
  │←─把结果拼回对话──    │
  │                     │──沉思：结果够了，回答──→ 你
  │←─"Final Answer"──   │
  │                     │
```

**你的代码负责执行，LLM 负责决策。** 这就是所谓"工具调用"（tool use）的分工。

---

## 4. 为什么 LLM 每轮都"失忆"？—— 上下文窗口

LLM 是一个**无状态**的函数。你问它一个问题，它给一个回答，然后**它什么都不记得**了。

所以你必须**把之前所有的对话都重新发给它**。这就是 `messages`（对话历史）列表的作用：

```python
messages = [
    {"role": "user", "content": "北京有多少人"},
    {"role": "assistant", "content": "我需要先搜索"},   # 上一轮 LLM 说的话
    {"role": "user", "content": "Observation: 北京是..."}  # 上一轮工具的结果
]
```

每轮循环你都把 `messages` 整个发给 LLM（agent.py:136），它才能"假装记得"之前发生了什么。

**这带来一个关键问题**：对话越长，每轮发的 token 就越多，成本越高。这就是后面阶段 7 做"成本控制 / 上下文窗口管理"的原因。

---

## 5. 提示词（System Prompt）为什么叫"系统提示词"

在 `messages` 里，`role: "system"` 是给 LLM 的**总规则**，优先级最高，它规定了一件事：**LLM 应该以什么身份、按什么格式工作**。

看 agent.py:23-44，你的 system prompt 说了三件重要的事：
1. 有哪些工具可用（`get_tool_descriptions()`）
2. 必须按 `Thought / Action / Action Input` 这个格式输出
3. **不要编造工具结果**，等真实的 Observation

第 3 条很重要：LLM 有时会"说谎"（幻觉），自己编一个搜索结果而不等你执行。你在 API 调用里加了 `stop=["Observation:"]`（agent.py:138）来阻止这一点——意思是"LLM 一输出 Observation 就截断"，逼它把结果留给程序填。

---

## 6. 正则表达式：把你的"意图"从 LLM 的废话里抠出来

LLM 输出的是**一大段文本**，比如：

```
Thought: 我需要搜索
Action: search
Action Input: 北京
```

你的程序需要从这段文本里**取出**"action = search，参数 = 北京"。这就是正则表达式干的活。

`re.search(pattern, text)` 在文本里找匹配。关键语法（对应 agent.py:58）：

| 符号 | 含义 |
|---|---|
| `\s` | 空白（空格/换行/tab） |
| `\s*` | 空白出现任意次 |
| `.+?` | 任意字符，非贪婪（尽量少，遇到边界就停） |
| `(?:\n|$)` | 遇到换行或结尾就停（这就是"只取这一行"的边界） |
| `group(1)` | 取括号里捕获的内容 |

例：`re.search(r"Action:\s*(.+?)(?:\n|$)", text)` 对上面的文本，`group(1)` = `"search"`。

---

## 7. 三种响应：final_answer / action / error

`_parse_llm_response` 把 LLM 的原始文本分类成三种，代码里靠 `parsed["type"]` 分发（agent.py:148/163/184）：

| type | 含义 | 代码动作 |
|---|---|---|
| `final_answer` | LLM 说"我可以回答了" | 结束循环，返回答案（agent.py:148） |
| `action` | LLM 想调工具 | 执行工具，继续循环（agent.py:163） |
| `error` | 解析失败（格式不对） | 喂回去让它重新输出（agent.py:184） |

**为什么要有 error 分支**：LLM 偶尔不按格式来（漏了字段、多用词、甚至输出乱码）。你不能因此就崩溃，而要把"你输出错了"这个信息告诉它，让它自我修正。这就是"错误恢复"——**错误也是一个可喂回去的信息，而不是一个终止信号。**

---

## 8. 工具系统：工具注册表（TOOLS 字典）

`app/tools.py` 里：

```python
TOOLS = {
    "calculator": calculator,   # 键 = 工具名，值 = 函数
    "search": search,
}
```

这个字典叫**注册表**——它记录了"有哪些工具、每个工具叫什么、对应的函数是谁"。

`get_tool_descriptions()` 把工具名和说明整理成一段文本，放进 system prompt 让 LLM 知道能用啥。

`_call_tool()`（agent.py:163-176）执行工具时，先检查名字在不在注册表里（`tool_name in TOOLS`），不在就返回错误字符串——**工具失败不抛异常，而是变成 Observation 喂回给 LLM**（呼应第 7 节"错误也是信息"）。

---

## 9. 防烧钱：为什么必须有限制

Agent 循环有个致命风险：**LLM 可能一直循环下去**，每轮都烧 token，钱就流走了。所以有三层防线（面试常问"如何防止 Agent 无限循环烧钱"）：

| 防线 | 代码 | 说明 |
|---|---|---|
| 1. 硬上限 | `for iteration in range(max_iterations)`（agent.py:131） | 最多循环 N 次，到了就停 |
| 2. 重复调用检测 | agent.py:166-168 | 如果连续两次调用相同工具+相同参数，警告 LLM 换思路 |
| 3. 预算上限 | （阶段 7 实现在 agent.py 之外） | 累计 token 成本超过预算就终止 |

面试时把这三层讲出来，比只答"设置个最大次数"要加分很多。

---

## 10. 执行轨迹（trajectory）：为什么每一步都要记下来

`self.trajectory` 是一个列表，把每一步发生的事都记下来（agent.py:170-176）。为什么要记？

1. **调试**：出问题时，你能看到 LLM 每步在想什么、干了什么。
2. **展示**：前端（阶段 4 的 SSE）把 trajectory 逐条渲染，用户就能看到实时进度。
3. **审计 + 回放**：阶段 3 把 trajectory 落库后，能回放整个执行过程。
4. **计量**：每步的 token 消耗、耗时，都挂在 trajectory 上（阶段 7）。

> 注意：**连 error 也要记**（agent.py:194-197）。如果失败的尝试不记录，你就永远不知道"这个任务为什么重试了这么多次"。可观测性要求记录所有事件，尤其是失败的。

---

## 11. 端到端流程总图（对照 agent.py）

```
POST /agent/run
   ↓
AgentExecutor.run(user_input)              [main.py → agent.py:111]
   ↓
for iteration in range(max_iterations):    [agent.py:131]  ← 主循环
   ↓
调用 DeepSeek API（发 system + messages） [agent.py:134-139]
   ↓
拿到 LLM 原始文本 llm_response_text         [agent.py:140]
   ↓
_parse_llm_response() 解析成三种类型        [agent.py:145 → 46]
   ↓
final_answer?  ──yes──→ return 答案        [agent.py:148]
   ↓ no
action?        ──yes──→ _call_tool()       [agent.py:163]
   ↓              ├── 结果 → 记录 trajectory
                    ├── 结果 → 拼回 messages  [agent.py:180-181]
                    └── 下一轮循环
error?         ──yes──→ 喂回去让它重来       [agent.py:184]
   ↓
（循环结束）return "达到最大迭代次数"        [agent.py:202]
```

---

## 12. 阶段 1 面试题自检（脱稿能讲 2 分钟）

1. **手写 Agent loop 和用 LangGraph 的区别？**
   答：我的 loop 每步的职责是……（讲 Thought/Action/Observation 怎么映射、状态存哪、错误怎么恢复）。LangGraph 把状态、图、持久化都抽象好了，我可以对着它的概念说我的哪行代码对应它的哪个概念。

2. **任务失败怎么办？**
   答：格式错误 → 喂回去重试（agent.py:184）；工具失败 → 变 Observation 不崩（agent.py:105）；都失败 → 靠 max_iterations 兜底。后续加断点续跑和人工介入。

3. **如何防止 Agent 无限循环烧钱？**
   答：max_iterations 硬上限 + 重复调用检测 + 预算上限（见第 9 节三防线）。

4. **为什么每一步都要记 trajectory？**
   答：调试 / 前端展示 / 落库审计回放 / token 计量（见第 10 节四条）。

---

## 13. 你踩过的坑（这些是宝贵经验）

| 坑 | 教训 |
|---|---|
| `action_input.group(1)` → NameError | 变量名别写错，`action` 对应 `action_match` |
| `llm_response_text` 后面被 `"模拟响应"` 覆盖 | 赋值语句有先后顺序，后面的覆盖前面的 |
| 第一轮 `trajectory[-1]` → IndexError | 空列表取 `[-1]` 会崩，先判断非空 |
| `/n` 不是换行，`\n` 才是 | 反斜杠才代表转义 |
| `&&` 在 Python 里语法错误 | 逻辑与用 `and` |
| `trajectory[action]` → KeyError | 字典取键要带引号 `["action"]` |

---

## 14. 两个值得记住的类比

- **LLM = 只会说话的军师**：它负责出主意（用哪个工具、怎么算），但不会亲自打仗。真正打仗（执行工具）的是你的代码。
- **messages = 军师的记事本**：军师每说完一句，你把它记下来；下一轮开始时把记事本整个读给它听，它才知道之前聊到哪了。

---

*阶段 1 到此完整。下一阶段：function calling 重构（让 LLM 直接返回结构化的 JSON，替代脆弱的文本解析）。*

---

# 阶段 2：Function Calling（工具调用的"正规军"版本）

## 15. 为什么文本解析很脆弱

阶段 1 靠正则从 LLM 的文本里抠信息，有三个天生缺陷：

1. **格式不稳定**：LLM 偶尔输出 `**Action:**`（markdown 加粗）、`Action：`（中文冒号）、或者漏掉 `Action Input`，正则就匹配不到。
2. **只能传字符串**：你没法告诉 LLM "calculator 需要一个叫 `expression` 的参数，类型是字符串"，只能在 prompt 里写自然语言描述。
3. **需要防幻觉**：LLM 可能自己编造 `Observation: ...`，要用 `stop=["Observation:"]` 拦住。

**Function Calling 的解法**：不让 LLM 输出文本，而是返回**结构化的 JSON 对象**。你给 API 传工具的 JSON Schema，API 保证返回合法格式，不用再解析。

## 16. Function Calling 的核心流程（对比阶段 1）

| 步骤 | 阶段1 文本解析 | 阶段2 Function Calling |
|---|---|---|
| 1. 传工具描述 | 写在 system prompt 字符串里 | `tools=TOOL_SCHEMAS` 作为 API 参数 |
| 2. LLM 返回 | 文本：`"Action: search\nAction Input: 北京"` | 对象：`message.tool_calls[0].function.name = "search"` |
| 3. 解析 | 正则 `re.search(r"Action:\s*(.+?)")` | 直接取 `.name` `.arguments`，无需正则 |
| 4. 参数传递 | 字符串 `"北京"` | dict `{"query": "北京"}` 用 `**dict` 展开 |
| 5. 结果回传 | `role: "user", content: "Observation: ..."` | `role: "tool", tool_call_id: xxx, content: ...` |

## 17. JSON Schema：给工具写"说明书"

在 tools.py 里定义 `TOOL_SCHEMAS`（agent_fc.py:3 导入为 `tools`）：

```python
tools = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "计算数学表达式，如 2+3*4",   # LLM 看这个决定用不用此工具
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {                       # 参数名
                        "type": "string",                 # 参数类型
                        "description": "要计算的数学表达式，如 '2+3*4'"
                    }
                },
                "required": ["expression"]                # 必填参数
            }
        }
    }
]
```

**description 为什么重要**：这是给 LLM 看的"工具说明书"。写得越精确，LLM 选工具越准。只写"计算"，LLM 不知道是算数还是算钱；写清楚"计算数学表达式，支持加减乘除和括号"，它才会正确使用。

## 18. `**dict` 展开语法（Python 必会）

Function Calling 里 `arguments` 是 JSON 字符串（如 `'{"query": "北京"}'`），要 `json.loads` 解析成 dict：

```python
tool_args = json.loads(tool_call.function.arguments)   # → {"query": "北京"}
```

调用工具时用 `**tool_args` **展开**成关键字参数：

```python
TOOLS["search"](**tool_args)
# 等价于
TOOLS["search"](query="北京")
```

`**` 把字典的键值对拆开，作为 `参数名=参数值` 传入函数。这是 Python 的"字典解包"语法。

## 19. `role: "tool"` 消息（区别于阶段 1）

阶段 1 把工具结果伪装成 `role: "user"` 消息（`"Observation: ..."`）。Function Calling 有专门的 `role: "tool"`：

```python
messages.append({
    "role": "tool",
    "tool_call_id": tool_call.id,   # 配对：告诉 API 这是哪个 tool_call 的结果
    "content": observation,
})
```

**为什么要配对**：一次 LLM 响应可能返回多个 tool_calls（并行调工具），每个 tool 消息都要通过 `tool_call_id` 配对，告诉 API "这是哪个调用的结果"。

**顺序也很重要**：必须**先追加 LLM 的原始消息**（`messages.append(message)`，含 tool_calls），**再追加 tool 消息**。否则 API 报错。

## 20. 两种结束条件

Function Calling 版的循环判断（agent_fc.py:63-100）：

```python
if message.tool_calls:
    # LLM 决定调工具 → 执行，回传，继续循环
elif message.content:
    # LLM 直接回答 → 结束循环，返回答案
```

**注意**：`message.content` 和 `message.tool_calls` **互斥**。LLM 要么返回工具调用，要么返回文本答案，不会同时有两者。这是 Function Calling 协议的规定。

## 21. 不再需要 `stop=["Observation:"]`

阶段 1 要用 `stop` 防止 LLM 自己编 Observation。Function Calling 模式下**不需要**——因为工具结果通过 `role: "tool"` 消息回传，LLM 根本没有机会输出 "Observation" 这个词。

## 22. 面试对比题："文本解析 vs Function Calling，各适合什么场景？"

| | 文本解析 | Function Calling |
|---|---|---|
| **可靠性** | 靠 prompt 约束，会跑偏 | API 层保证格式 |
| **参数类型** | 只有字符串 | 支持类型校验 |
| **可移植性** | 任何 LLM 都能用 | 依赖厂商 API 能力 |
| **适合场景** | 快速原型、教学、不支持 tools 的模型 | 生产环境、参数复杂、需要稳定性 |

**标准答法**：两种我都实现过。文本解析帮我理解 Agent 循环的本质（解析→调工具→喂回去），但不适合生产。Function Calling 把"格式可靠性"交给 API 层，让我专注业务逻辑。如果厂商支持 tools 参数，优先用 Function Calling。

---

## 🎓 阶段 2 收尾：你学到了什么

- ✅ 知道 Function Calling 为什么比文本解析可靠（API 层保证格式 vs 正则匹配）
- ✅ 会写 JSON Schema 定义工具（`properties` / `required` / `description`）
- ✅ 理解 `**dict` 展开语法（Python 基础）
- ✅ 知道 `role: "tool"` 和 `tool_call_id` 配对的作用
- ✅ 能对比两种实现，讲清各自优劣（面试必备）

---

**下一阶段预告**：阶段 3 状态持久化 + 断点续跑——把 trajectory 落库（PostgreSQL/SQLite），让任务可审计、可重试、可恢复。这是"任务失败怎么办"面试题的核心能力。

*阶段 2 完成。两个版本的 Agent 已全部跑通。*

---

# 阶段 3：状态持久化 + 断点续跑（数据库 + 恢复能力）

## 23. 为什么内存状态不够用

阶段 1-2 的 `self.trajectory` 是 Python 列表，进程一退出就没了。三个致命问题：

1. **不可审计**：任务跑完后无法回看"它调了哪些工具、每步结果是啥"
2. **不可恢复**：进程崩溃（网络断、LLM 超时）→ 前面的钱白花，要从头再来
3. **不可重试**：某一步失败 → 整个任务失败，无法"只重跑失败的那一步"

**持久化的解法**：每一步**写入数据库**，状态在数据库里，不在内存里。

## 24. 表设计：tasks + steps 两张表

```sql
tasks 表（任务级）:
  - id (UUID)              -- 任务唯一标识
  - question               -- 用户提交的问题
  - status                 -- PENDING/RUNNING/COMPLETED/FAILED（状态机）
  - answer                 -- 最终答案
  - max_iterations         -- 最大循环次数
  - created_at / updated_at

steps 表（步骤级）:
  - id (UUID)
  - task_id                -- 关联到 tasks.id
  - iteration              -- 第几轮循环
  - type                   -- "tool_call" 或 "final_answer"
  - tool_name / tool_args  -- 工具调用的参数
  - observation            -- 工具执行结果
  - started_at / finished_at
  - UNIQUE (task_id, iteration)  ← 幂等关键！
```

**为什么需要两张表**：tasks 存"任务整体"（问了什么、最终答案是啥），steps 存"每一步细节"（调了什么工具、结果是啥）。一对多关系。

## 25. 状态机：任务只能沿箭头转移

```
PENDING → RUNNING → COMPLETED
            ↓
          FAILED
```

规则：
- 只能沿箭头转移（如 PENDING → RUNNING 合法，COMPLETED → RUNNING 非法）
- 每次转移都写数据库
- 非法转移要拒绝（应用层或数据库约束）

**为什么要状态机**：防止状态混乱（如任务已完成却还在跑、任务失败了却标记成功）。状态机是"任务生命周期管理"的基础。

## 26. 幂等设计：`UNIQUE (task_id, iteration)` 的作用

这个约束保证：**同一步不会被插入两次**。

**场景**：重试逻辑有 bug，某一步被执行了两次 → 第二次插入时，数据库抛 `IntegrityError` → 暴露问题，而不是静默产生脏数据（两条 iteration=2 的记录）。

**这就是幂等**：同样的操作执行多次，效果和执行一次相同。数据库约束是幂等的**最后一道防线**。

**面试加分**：讲到这里能说"幂等设计在数据库层的体现"，比只说"避免重复"要深一层。

## 27. 每步落库的时机（关键顺序）

在 [agent_fc_persistent.py](app/agent_fc_persistent.py) 里：

1. **任务开始**：插入 tasks 表（status = "RUNNING"）
2. **每步执行完**：立即插入 steps 表（observation、finished_at）
3. **任务结束**：更新 tasks 表（status = "COMPLETED"，answer）

**关键原则**：**先落库再继续**。如果"执行完所有步骤再一起写库"，进程中途崩溃就什么都没留下。

## 28. 断点续跑（Resume）的核心逻辑

从数据库恢复任务并继续执行，需要做四件事：

1. **读任务信息**：question、status、max_iterations
2. **读已完成的 steps**：按 iteration 排序
3. **重建上下文**：把已完成步骤的信息传给 LLM（有两种方式，见下）
4. **从断点继续**：从 `len(completed_steps) + 1` 这一轮开始循环

**两种重建上下文的方式**：

| 方式 | 实现 | 优点 | 缺点 |
|---|---|---|---|
| A. 重建 messages | 从 steps 表反向构造 assistant + tool 消息 | LLM 看到完整对话历史 | 需要存 LLM 原始 message（含 reasoning_content 等厂商特有字段） |
| B. 上下文摘要 | 把已完成步骤写成文字摘要，塞进 system prompt | 简单，不依赖消息格式 | LLM 只看到"摘要"，不是真实对话 |

**当前实现用的方案 B**（简化版），因为 DeepSeek v4 的 reasoning_content 字段需要严格回传，方案 A 需要改表设计存完整 message。

**生产级建议**：用方案 A，在 steps 表加一列 `llm_message_json TEXT` 存 LLM 的原始响应，resume 时直接用。

## 29. 断点续跑省的是什么

假设任务执行到第 3 步时崩溃，前面已完成：
- 第 1 步：search，花了 500 tokens
- 第 2 步：calculator，花了 300 tokens

**不续跑**：从头再来 → 又要花 500 + 300 = 800 tokens 重复执行已完成的步骤。

**续跑**：直接从第 4 步开始 → 不重复执行，省下 800 tokens 和时间。

**这就是断点续跑的价值**：不重复烧钱，不丢进度。

## 30. 面试题："任务失败怎么办？"的完整答案

现在你能完整回答这道高频题：

**三层处理**：
1. **工具调用失败** → 返回错误字符串（不崩溃），喂回 LLM 让它重试或换工具（阶段 1）
2. **任务执行中断** → 每步落库，重启后 resume 从断点继续（阶段 3）
3. **最终仍失败** → 状态标记 FAILED，记录到数据库，支持人工查看 trajectory 排查原因（阶段 3），后续可加人工审批（阶段 6）

**配合讲**：幂等设计（`UNIQUE` 约束）+ 状态机（防止状态混乱）+ 断点续跑（省成本）。

## 31. 从 SQLite 到 PostgreSQL 的迁移路径

当前用 SQLite（单文件数据库）起步，生产环境换 PostgreSQL 只需：

1. 改连接字符串（`sqlite3.connect` → `psycopg2.connect`）
2. 改 `datetime('now')` → `NOW()`（SQL 方言差异）
3. UUID 类型：SQLite 用 TEXT 存，PostgreSQL 有原生 UUID

**表结构完全一样**，不需要重新设计。这就是"先用轻量的跑通，再换生产级"的好处。

---

## 🎓 阶段 3 收尾：你学到了什么

- ✅ 理解状态持久化的三大价值（审计 / 恢复 / 重试）
- ✅ 会设计 tasks + steps 两张表，理解一对多关系
- ✅ 理解状态机和幂等设计（`UNIQUE` 约束的作用）
- ✅ 实现了 resume 方法，能从数据库恢复任务并继续
- ✅ 能完整回答"任务失败怎么办"面试题（三层 + 配套机制）

---

**下一阶段预告**：阶段 4 异步任务 + SSE 实时推送——让任务后台跑，前端通过 Server-Sent Events 实时看进度。这是"长任务"场景的标准架构。

*阶段 3 完成。状态持久化和断点续跑已全部验证通过。*
