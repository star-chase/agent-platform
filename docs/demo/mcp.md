# MCP 端到端演示：平台（客户端）→ rag-agent（服务端）

```
agent-platform Worker
  └─ Agent 循环 → TOOLS["mcp__rag__search_knowledge"]
        └─ MCP client（stdio，换行分隔 JSON-RPC）
              └─ rag-agent MCP server（python -m app.mcp.server）
                    └─ 混合检索（pgvector + 关键词 + RRF + 精排）
        ← content[].text（片段 + 来源文件名 + 分数）
```

两边都是**手写的协议层**（不依赖官方 SDK），所以这份记录同时也是协议实现的验收证据。

## 复现

```bash
# 1) rag-agent：灌演示数据（demo 用户 + 企业制度演示库）
cd /home/asus/projects/rag-agent
PYTHONPATH=.deps python3 -m app.db.seed_demo

# 2) agent-platform：以 MCP 客户端身份调用 rag-agent 的检索工具
cd /home/asus/projects/agent-platform
MCP_USER_EMAIL=demo@example.com MCP_DEFAULT_KB_ID=1 \
    python3 scripts/demo/mcp_demo.py "年假有多少天"
```

`scripts/demo/mcp_demo.py` 刻意**不经过 LLM**：它直接调用平台真正执行工具的那条路径
（`app.tools.TOOLS`，Agent 循环调的就是它），所以验证的是"接线 + 协议 + 检索"，
而不是"模型会不会挑工具"。

## 实测输出（2026-10-10，本机）

### 1) 启动时握手并注册工具

```text
== 1) 平台启动时注册 MCP 工具（握手 + tools/list）==
注册结果： ['mcp__rag__search_knowledge', 'mcp__rag__ask_knowledge', 'mcp__rag__list_knowledge_bases']
  server=rag command=['python3', '-m', 'app.mcp.server'] cwd=/home/asus/projects/rag-agent

```

### 2) 调用外部检索工具

```text
== 2) 调用外部检索工具：TOOLS['mcp__rag__search_knowledge'](query='年假有多少天') ==
知识库 1 命中 5 个片段：
[1] overtime.md（score=0.0164）
# 加班与调休

加班需提前在考勤系统申请并获得主管审批，工作日加班满 2 小时可申请调休，法定节假日加班按 3 倍工资计发。

调休需在加班后的 60 天内使用，逾期作废。每月加班时长超过 36 小时需要部门负责人额外审批。

加班餐补为每次 30 元，晚上 21:00 后打车费用可报销。
```

### 3) 带引用的问答（另一个工具）

```text
== 3) 带引用的回答：TOOLS['mcp__rag__ask_knowledge'](question='年假有多少天') ==
根据知识库，关于“年假有多少天”的回答如下：

[1] (来源: overtime.md)

参考资料：[1]
```

## 这次演示暴露的两个真实问题（都已修）

1. **stdout 污染协议**：rag-agent 的 `.env` 里 `DEBUG=true` → SQLAlchemy 的 `echo` 把每条 SQL
   打到 **stdout**；而 MCP 的 stdout 是协议通道。第一版 client 只能"跳过非 JSON 行"兜底，
   属于容错不是正确。修法是服务端**fd 级接管 stdout**：`dup` 出协议专用流，再用 `os.dup2`
   把 fd 1 指向 stderr —— 只换 `sys.stdout` 拦不住（SQLAlchemy 在 import 期就抓住了原始对象）。
   两个仓库都补了 fd 级回归测试。
2. **检索没有相关性阈值**：问"年假"时返回的候选里也混着加班、密码策略等片段
   （离线 fake embedding 放大了这个问题）。这与 `evals/report.md` 的结论一致：
   拒答/过滤目前依赖提示词，下一步应加相关性阈值或"检索为空即拒答"的短路分支。
