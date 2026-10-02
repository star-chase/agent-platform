"""阶段 8-B：集中式配置。

测试的重点不是"能不能读到环境变量"（那是库的功能），而是四件**我们自己的决策**：

  1. 默认值对（不设任何环境变量时，项目能照常跑起来）
  2. **类型不对时启动即失败** —— 这才是从 `os.getenv` 换过来的真正收益：
     `float(os.getenv(..., "2"))` 要等到第一次 tick 才崩，配置类在构造时就崩
  3. 缓存语义明确（同一个进程里是常量；改了环境变量必须显式失效）
  4. 启动日志里**不含密钥** —— 日志系统的留存/权限/导出范围比密钥管理宽松得多
"""

import pytest
from pydantic import ValidationError

from app.config import Settings, describe_settings, get_settings, reset_settings_cache

ALL_ENV = (
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
    "DATABASE_URL", "DATABASE_PATH", "REDIS_URL",
    "ORCHESTRATOR_POLL_INTERVAL", "STALE_TASK_MINUTES", "LOG_PATH",
)


@pytest.fixture
def clean_env(monkeypatch):
    """清空环境变量 + 关掉 .env 文件，让"默认值"这件事真的可测。

    为什么要连 .env 一起关掉：pydantic-settings 默认会读它。开发者本机 `.env` 里
    恰好设了某个变量，测试就会"在本机全绿、在 CI 红" —— 而默认值测试的全部意义
    就是验证"什么都不配也能跑"。**测试要能控制自己的环境，不能借用开发者的环境。**
    """
    for name in ALL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)   # monkeypatch 结束后自动还原
    reset_settings_cache()
    yield
    reset_settings_cache()


# ---------------------------------------------------------------------------
# B1：默认值 —— 什么都不配也能跑
# ---------------------------------------------------------------------------

def test_defaults_are_usable_without_any_env(clean_env):
    settings = get_settings()

    assert settings.deepseek_base_url == "https://api.deepseek.com/v1"
    assert settings.deepseek_model == "deepseek-v4-flash"
    assert settings.database_path == "agent.db"
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.orchestrator_poll_interval == 2.0
    assert settings.stale_task_minutes == 15
    assert settings.dialect == "sqlite"          # 没配 URL → 回落到 SQLite


def test_env_overrides_defaults(clean_env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MODEL", "别的模型")
    monkeypatch.setenv("ORCHESTRATOR_POLL_INTERVAL", "0.5")
    monkeypatch.setenv("DATABASE_PATH", "/data/x.db")
    reset_settings_cache()

    settings = get_settings()

    assert settings.deepseek_model == "别的模型"
    assert settings.orchestrator_poll_interval == 0.5      # 字符串 → float，自动转换
    assert settings.database_path == "/data/x.db"


# ---------------------------------------------------------------------------
# B2：配置错误在**启动时**炸，而不是在第一次用到它的时候炸
# ---------------------------------------------------------------------------

def test_bad_type_fails_fast(clean_env, monkeypatch):
    """这是换 pydantic-settings 的核心收益，不是"代码更整齐"。

    读代码时它就有：
        float(os.getenv("ORCHESTRATOR_POLL_INTERVAL", "2"))   # 写成 abc 要到第一次 tick 才崩
    配置类在 **构造那一刻** 就拒绝，进程根本起不来 —— 错误提前到"部署时"而不是"半夜"。
    """
    monkeypatch.setenv("ORCHESTRATOR_POLL_INTERVAL", "abc")
    reset_settings_cache()

    with pytest.raises(ValidationError):
        get_settings()


def test_blank_env_value_means_unset(clean_env, monkeypatch):
    """.env 里的 `LOG_PATH=` 给到的是空字符串，不是 None —— 必须归一掉。

    真实事故：容器部署时 orchestrator + worker 三个容器全部崩溃重启，
    报的是 `IsADirectoryError: [Errno 21] Is a directory: '/app'` ——
    因为 `setup_logging("")` 去打开了一个空路径（落在当前工作目录上）。
    注意它**只在容器里炸**：本地跑时 .env 里那一行是注释掉的。
    """
    monkeypatch.setenv("LOG_PATH", "")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    reset_settings_cache()

    settings = get_settings()

    assert settings.log_path is None          # 空 → 未设置
    assert settings.database_url is None
    assert settings.deepseek_api_key is None
    assert settings.dialect == "sqlite"       # 空的 DATABASE_URL 不能当成 PG


# ---------------------------------------------------------------------------
# B3：缓存语义
# ---------------------------------------------------------------------------

def test_settings_are_cached_within_a_process(monkeypatch):
    """同一个进程里配置是常量：反复取到的是同一个对象（不是每次重新解析 env）。"""
    reset_settings_cache()
    first = get_settings()

    assert get_settings() is first


def test_reset_makes_it_reread_the_environment(monkeypatch):
    """改了环境变量必须显式失效 —— 缓存是决策，不是免费的午餐。"""
    monkeypatch.setenv("DEEPSEEK_MODEL", "模型甲")
    reset_settings_cache()
    assert get_settings().deepseek_model == "模型甲"

    monkeypatch.setenv("DEEPSEEK_MODEL", "模型乙")
    assert get_settings().deepseek_model == "模型甲"        # 没失效 → 还是旧值

    reset_settings_cache()
    assert get_settings().deepseek_model == "模型乙"        # 失效之后才对


def test_dialect_follows_database_url(clean_env, monkeypatch):
    cases = [
        (None, "sqlite"),
        ("postgresql://u:p@h/db", "postgres"),
        ("postgres://u:p@h/db", "postgres"),
        ("sqlite:///tmp/x.db", "sqlite"),
    ]
    for url, expected in cases:
        if url is None:
            monkeypatch.delenv("DATABASE_URL", raising=False)
        else:
            monkeypatch.setenv("DATABASE_URL", url)
        reset_settings_cache()
        assert get_settings().dialect == expected, url


# ---------------------------------------------------------------------------
# B4：启动快照不能泄漏密钥
# ---------------------------------------------------------------------------

def test_describe_settings_never_leaks_the_api_key(clean_env, monkeypatch):
    """启动时打印配置是排障利器，但**密钥一旦进了日志就等于写进了日志系统**。

    日志的留存周期、谁能看、会被导出到哪儿，通常比密钥管理系统宽松得多。
    所以快照里只报"设没设"，不报"是什么"。
    """
    secret = "sk-绝密-不该出现在日志里"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    reset_settings_cache()

    snapshot = describe_settings()

    assert snapshot["deepseek_api_key_set"] is True
    assert secret not in str(snapshot)


def test_describe_settings_reports_the_effective_database(clean_env, monkeypatch):
    """快照要能回答"这个进程到底连的哪个库" —— 8-0 那个 bug 正是查不出这一点。"""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@postgres:5432/agent_platform")
    monkeypatch.setenv("DATABASE_PATH", "/tmp/should-be-ignored.db")
    reset_settings_cache()

    snapshot = describe_settings()

    assert snapshot["dialect"] == "postgres"
    assert snapshot["database_url"].startswith("postgresql://")
    assert snapshot["database_path"] is None      # 有 URL 时 SQLite 路径无效，别显示误导信息
