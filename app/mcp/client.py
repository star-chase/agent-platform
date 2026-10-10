"""MCP 客户端：把外部 MCP server 的工具接进本平台。

四条会被测试钉住的生命周期约定：
1. **懒启动**：第一次用到才 spawn，进程常驻；
2. **每 server 一把锁**：stdio 是单通道，并发写会把协议写坏，所以调用串行化；
3. **超时即杀**：超时后 kill 子进程并标记断开，下次调用自动重启 ——
   一个卡死的外部工具不该拖死整条 workflow；
4. **stderr 单独收**：stdout 只能跑协议，诊断信息必须走 stderr；收到的脏行记日志但不致命。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.mcp import protocol

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TIMEOUT_SECONDS = 30.0
CLIENT_INFO = {"name": "agent-platform", "version": "0.1.0"}


class MCPClientError(RuntimeError):
    """客户端侧失败：起不来、超时、协议不合法、子进程退出。"""


@dataclass
class MCPServerConfig:
    name: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    allowed_tools: list[str] | None = None
    cwd: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> MCPServerConfig:
        name = raw.get("name")
        command = raw.get("command")
        if not isinstance(name, str) or not name:
            raise MCPClientError("MCP server 配置缺少 name")
        if isinstance(command, str):
            command = [command]
        if not isinstance(command, list) or not command:
            raise MCPClientError(f"MCP server {name} 的 command 必须是数组")
        return cls(
            name=name,
            command=[str(part) for part in command],
            env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
            timeout_seconds=float(raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
            allowed_tools=raw.get("allowed_tools"),
            cwd=raw.get("cwd"),
        )


class MCPClient:
    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._next_id = 0
        self._tools: list[dict[str, Any]] | None = None

    # ---------- 生命周期 ----------

    def _running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def ensure_started(self) -> None:
        with self._lock:
            if self._running():
                return
            self._spawn()

    def _spawn(self) -> None:
        env = {**os.environ, **self.config.env}
        try:
            self._process = subprocess.Popen(
                self.config.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
                cwd=self.config.cwd,
            )
        except OSError as exc:
            raise MCPClientError(f"MCP server {self.config.name} 启动失败：{exc}") from exc

        # 队列与进程一起绑定：重启后旧线程仍然往**旧队列**写 EOF，
        # 否则它会把 EOF/残留行灌进新会话，下一次调用就直接"子进程已退出"。
        lines: queue.Queue[str | None] = queue.Queue()
        self._lines = lines
        process = self._process
        threading.Thread(
            target=self._pump_stdout, args=(process, lines), name=f"mcp-out-{self.config.name}", daemon=True
        ).start()
        threading.Thread(
            target=self._pump_stderr, args=(process,), name=f"mcp-err-{self.config.name}", daemon=True
        ).start()

        self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": CLIENT_INFO,
            },
        )
        self._notify("notifications/initialized")
        logger.info("mcp_server_started", extra={"server": self.config.name})

    def _pump_stdout(self, process: subprocess.Popen[str], lines: queue.Queue[str | None]) -> None:
        assert process.stdout
        for line in process.stdout:
            lines.put(line)
        lines.put(None)  # EOF：告诉等待方"这个子进程没了"

    def _pump_stderr(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr
        for line in process.stderr:
            logger.debug("mcp_server_stderr", extra={"server": self.config.name, "line": line.rstrip()})

    def close(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process is None:
            return
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001 —— 关不掉也别抛，进程退出时会被回收
            logger.warning("mcp_server_close_failed", extra={"server": self.config.name})

    def _kill(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            try:
                process.kill()
            except Exception as exc:  # noqa: BLE001 —— 杀不掉也要继续，进程退出时会被回收
                logger.debug(
                    "mcp_server_kill_failed",
                    extra={"server": self.config.name, "error": str(exc)},
                )

    # ---------- 协议 ----------

    def _send(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise MCPClientError(f"MCP server {self.config.name} 未启动")
        try:
            self._process.stdin.write(protocol.dumps(message) + "\n")
            self._process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise MCPClientError(f"写入 stdio 失败：{exc}") from exc

    def _read_result(self, request_id: Any, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPClientError(f"MCP server {self.config.name} 响应超时（{timeout}s）")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise MCPClientError(f"MCP server {self.config.name} 响应超时（{timeout}s）") from exc
            if line is None:
                raise MCPClientError(f"MCP server {self.config.name} 子进程已退出")
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # 容忍脏 stdout（有的 server 会误把日志写进 stdout），但要留痕——这是配置问题。
                logger.warning("mcp_stdout_not_json", extra={"server": self.config.name, "line": line[:200]})
                continue
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                raise MCPClientError(
                    f"MCP 协议错误 {error.get('code')}: {error.get('message')}"
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise MCPClientError("MCP 响应缺少 result 对象")
            return result

    def _request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            try:
                self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
                return self._read_result(request_id, timeout or self.config.timeout_seconds)
            except MCPClientError:
                self._kill()  # 超时/崩溃后立刻断开，下次调用会自动重启
                raise

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        with self._lock:
            message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                message["params"] = params
            self._send(message)

    # ---------- 对外能力 ----------

    def list_tools(self, refresh: bool = False) -> list[dict[str, Any]]:
        if self._tools is None or refresh:
            self.ensure_started()
            result = self._request("tools/list", {})
            tools = result.get("tools")
            if not isinstance(tools, list):
                raise MCPClientError("tools/list 返回格式不正确")
            self._tools = [tool for tool in tools if isinstance(tool, dict) and tool.get("name")]
        return self._tools

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.ensure_started()
        return self._request("tools/call", {"name": tool_name, "arguments": arguments})


def extract_text(result: dict[str, Any]) -> str:
    """把 content 数组拼成文本：模型看到的是字符串，平台落库的 observation 也是字符串。"""
    parts = result.get("content") or []
    chunks = [
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return "\n".join(chunk for chunk in chunks if chunk)


__all__ = [
    "CLIENT_INFO",
    "DEFAULT_TIMEOUT_SECONDS",
    "PROTOCOL_VERSION",
    "MCPClient",
    "MCPClientError",
    "MCPServerConfig",
    "extract_text",
]
