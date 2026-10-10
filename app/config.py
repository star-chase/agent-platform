"""集中式配置（阶段 8-B）——全项目**唯一**读取环境变量的地方。

## 为什么要收拢

阶段 8 之前，`os.getenv` 散落在 **9 个文件、25 处**。这带来三种事故，全都真实发生过：

1. **漏读**：`app/main.py` 的单 Agent 端点写死 `sqlite3.connect("agent.db")`，
   完全绕过 `DATABASE_PATH` —— API 写一个库、worker 读另一个库，
   任务提交返回 202 然后**永远不执行、零报错**（2026-10-01 真实踩到，见 CLASS_LOG）。
2. **类型错误到运行时才炸**：`float(os.getenv("ORCHESTRATOR_POLL_INTERVAL", "2"))` ——
   写成 `abc` 的话，进程能启动、直到第一次 tick 才崩。
3. **默认值各写各的**：同一个 `DATABASE_PATH` 的默认值在 8 个地方各写一遍，改一处漏七处。

收成一个类之后，这三种问题在**导入/启动的一瞬间**就暴露，而不是在生产环境的某个半夜。

## 为什么配置类只认环境变量，不自己去找 `.env`（`env_file` 只作为开发便利）

**配置的来源应该由部署层决定**：本地是 `.env`、容器是 compose 的 `env_file`、
k8s 是 Secret/ConfigMap、CI 是流水线变量。应用只管"从环境变量读"这一件事，
换部署方式就不用改代码 —— 这就是 12 要素里的第三条（Config）。

（`env_file=".env"` 仍然配了：本地开发时 `python -m app.worker` 不必先 `source .env`。
它只是"把文件内容灌进环境变量"的便利，不是另一条配置通路。）

## 缓存与失效

`get_settings()` 带 `lru_cache`：配置在一个进程里是**常量**，
不该每次调用都重新解析 env + 校验一遍类型。
代价是"改了环境变量必须显式失效" —— `reset_settings_cache()` 就是给测试和热重载用的。
**缓存配置是一个决策，不是免费的午餐**，所以两个函数都写在这里，让人一眼看得见。
"""

import os
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """所有运行时配置。字段名即环境变量名（大小写不敏感）。

    每个字段都带类型：**配置错误在启动时炸，而不是在第一次用到它的时候炸**。
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- DeepSeek ----
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-v4-flash"

    # ---- 存储 ----
    # DATABASE_URL 有值 → PostgreSQL；否则用 DATABASE_PATH 指向的 SQLite 文件。
    database_url: str | None = None
    database_path: str = "agent.db"
    redis_url: str = "redis://localhost:6379/0"

    # ---- 编排 ----
    orchestrator_poll_interval: float = 2.0
    stale_task_minutes: int = 15
    # 孤儿 PENDING 任务重新入队的阈值（秒）。0 = 关闭：
    # 队列消息丢了（Redis 非正常宕机）而任务还是 PENDING 时，靠它按 DB 状态补偿重投。
    orphan_task_recovery_seconds: int = 0

    # ---- MCP（外部工具）----
    # JSON 数组：[{"name": "rag", "command": ["python", "-m", "app.mcp.server"],
    #             "env": {...}, "timeout_seconds": 20, "allowed_tools": [...]}]
    # 空 = 不注册任何外部工具，默认行为完全不变。
    mcp_servers: str | None = None

    # ---- 日志 ----
    # 留空 = 只写 stdout（容器里的标准做法）。本地设成 agent.log 方便 grep。
    log_path: str | None = None

    @field_validator("deepseek_api_key", "database_url", "log_path", mode="after")
    @classmethod
    def _blank_means_unset(cls, value: str | None) -> str | None:
        """把空字符串归一成 None。

        **`.env` 里的 `LOG_PATH=`（有等号、没值）和"根本不写这一行"不是一回事**：
        前者会给到 `""`，而代码里到处都是 `if x is not None` 这种判断，`""` 会一路通过去。
        真实后果（2026-10-01 容器部署时炸的）：`setup_logging(log_path="")` 打开了一个
        空路径 → `IsADirectoryError: [Errno 21] Is a directory: '/app'`，
        orchestrator 和 worker 三个容器全部崩溃重启。

        "空 = 未设置"是配置的通用语义，所以在这里**一次**归一掉，
        而不是在每个使用点写 `if x:` 去防御。
        """
        return value.strip() or None if isinstance(value, str) else value

    @property
    def dialect(self) -> str:
        """当前用哪个数据库方言。判断只看 URL 的 scheme —— 不猜、不试探连接。

        配置决定行为，行为可预测（测试里改环境变量 + 清缓存就能切换）。
        """
        if self.database_url and self.database_url.startswith(
            ("postgres://", "postgresql://")
        ):
            return "postgres"
        return "sqlite"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """读取配置（进程内缓存）。"""
    return Settings()


def reset_settings_cache() -> None:
    """让下一次 get_settings() 重新读环境变量。

    只在两种场景用：测试里 monkeypatch 了环境变量之后、以及需要热重载配置时。
    生产代码路径上不该出现它 —— 配置在一个进程生命周期里是常量。
    """
    get_settings.cache_clear()


def describe_settings(settings: Settings | None = None) -> dict[str, object]:
    """给启动日志用的一份配置快照。

    **故意不含任何密钥字段**：启动时把配置打出来是排障利器（"这个进程到底连的哪个库"），
    但把 API Key 打进日志就是把密钥写进了日志收集系统 —— 日志的留存、权限、
    导出范围通常比密钥管理系统宽松得多。
    """
    settings = settings or get_settings()
    return {
        "deepseek_base_url": settings.deepseek_base_url,
        "deepseek_model": settings.deepseek_model,
        "deepseek_api_key_set": bool(settings.deepseek_api_key),
        "dialect": settings.dialect,
        "database_url": settings.database_url,
        "database_path": None if settings.database_url else settings.database_path,
        "redis_url": settings.redis_url,
        "orchestrator_poll_interval": settings.orchestrator_poll_interval,
        "stale_task_minutes": settings.stale_task_minutes,
        "orphan_task_recovery_seconds": settings.orphan_task_recovery_seconds,
        "mcp_servers_configured": bool(settings.mcp_servers),
        "log_path": settings.log_path,
        "pid": os.getpid(),
    }
