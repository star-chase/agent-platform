#!/usr/bin/env python3
"""MCP 端到端演示：**平台作为 MCP 客户端**调用 **rag-agent 作为 MCP server**。

不需要 LLM：直接走平台真正执行工具的那条路径（`app.tools.TOOLS`），
所以它验证的是"接线 + 协议 + 检索"整条链路，而不是"模型会不会挑工具"。

用法：
    export MCP_USER_EMAIL=demo@example.com      # rag-agent 里已存在的用户
    export MCP_DEFAULT_KB_ID=1                  # 该用户的知识库 id
    python3 scripts/demo/mcp_demo.py "年假有多少天"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.mcp.bridge import register_mcp_tools, registered_servers, shutdown_mcp_clients  # noqa: E402
from app.tools import TOOLS  # noqa: E402


def build_config(
    rag_dir: Path, python: str, email: str, kb_id: str | None, server_pythonpath: str
) -> str:
    env = {"MCP_USER_EMAIL": email, "PYTHONPATH": server_pythonpath}
    if kb_id:
        env["MCP_DEFAULT_KB_ID"] = kb_id
    return json.dumps(
        [
            {
                "name": "rag",
                "command": [python, "-m", "app.mcp.server"],
                "cwd": str(rag_dir),
                "env": env,
                "timeout_seconds": 60,
            }
        ],
        ensure_ascii=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP 端到端演示（platform → rag-agent）")
    parser.add_argument("question", nargs="?", default="年假有多少天")
    parser.add_argument(
        "--rag-dir", default=os.environ.get("RAG_AGENT_DIR", "/home/asus/projects/rag-agent")
    )
    parser.add_argument("--python", default=os.environ.get("RAG_AGENT_PYTHON", "python3"))
    parser.add_argument(
        "--server-pythonpath", default=os.environ.get("RAG_AGENT_PYTHONPATH", ".deps")
    )
    args = parser.parse_args()

    email = os.environ.get("MCP_USER_EMAIL")
    if not email:
        print("缺少 MCP_USER_EMAIL（必须是 rag-agent 里已存在的用户）", file=sys.stderr)
        return 2

    rag_dir = Path(args.rag_dir).resolve()
    if not (rag_dir / "app" / "mcp" / "server.py").exists():
        print(f"rag-agent 目录里没有 app/mcp/server.py：{rag_dir}", file=sys.stderr)
        return 2

    config = build_config(rag_dir, args.python, email, os.environ.get("MCP_DEFAULT_KB_ID"), args.server_pythonpath)

    print("== 1) 平台启动时注册 MCP 工具（握手 + tools/list）==")
    registered = register_mcp_tools(config)
    print("注册结果：", registered or "（无 —— server 没起来）")
    if not registered:
        return 1
    for name, client in registered_servers().items():
        print(f"  server={name} command={client.config.command} cwd={client.config.cwd}")

    search_tool = "mcp__rag__search_knowledge"
    print(f"\n== 2) 调用外部检索工具：TOOLS['{search_tool}'](query={args.question!r}) ==")
    print(TOOLS[search_tool](query=args.question))

    ask_tool = "mcp__rag__ask_knowledge"
    if ask_tool in TOOLS:
        print(f"\n== 3) 带引用的回答：TOOLS['{ask_tool}'](question={args.question!r}) ==")
        print(TOOLS[ask_tool](question=args.question))

    shutdown_mcp_clients()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
