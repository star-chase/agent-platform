"""MCP（Model Context Protocol）支持：把外部 MCP server 的工具接进本平台。

模块划分：
- protocol.py  JSON-RPC 2.0 编解码 + 错误码（与 rag-agent 侧同一份实现，刻意复制而不是跨仓库依赖）
- client.py    外部 MCP server 的 stdio 客户端（懒启动 / 串行 / 超时杀进程重启）
- bridge.py    把 MCP 工具注册进 app.tools 的 TOOLS 与 function schema
- server.py    本平台自己作为 MCP server（对接 HTTP API）
"""
