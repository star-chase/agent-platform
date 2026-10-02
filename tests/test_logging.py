"""阶段 7-B：结构化日志（JSON + contextvars 上下文注入）。

9 条测试 = 这份日志格式的 9 条契约。最该看懂的是第 5 条：

    test_context_is_isolated_between_concurrent_tasks

它用全局 dict 实现会**失败** —— 那正是"为什么必须用 contextvars"的可执行证明，
也是整个 7-B 唯一需要真正理解（而不是记住）的知识点。
"""

import asyncio
import io
import json
import logging

import pytest

from app.logging_setup import (
    bind_context,
    clear_context,
    current_context,
    log_context,
    setup_logging,
)

LOGGER = logging.getLogger("app.test")


@pytest.fixture(autouse=True)
def clean_context():
    """每条测试前后都清上下文。

    contextvars 是**进程级隐式状态**：上一条测试绑了 task_id 不清掉，
    下一条测试就会莫名其妙地看到它。测试之间必须隔离。
    """
    clear_context()
    yield
    clear_context()


@pytest.fixture
def stream():
    """建一个内存流当日志出口，测试里直接读它（不落盘、不污染终端）。

    级别用 INFO（不是 DEBUG）是有意的：**第三方库也往根 logger 打日志**。
    asyncio 启动时会打一条 DEBUG 的 "Using selector: EpollSelector"，
    级别开到 DEBUG，它就会混进你的日志流里 —— 而结构化日志的消费者是解析器，
    多出一条没有业务字段的"外来事件"就足以让它崩
    （本文件真的因为这个红过两条测试：`who` KeyError + DEBUG 没被过滤）。

    生产做法：根 logger 设 INFO，再对特别吵的库单独提到 WARNING。
    """
    buffer = io.StringIO()
    setup_logging(level="INFO", stream=buffer)
    return buffer


def events(buffer):
    """把内存流里的 JSON 行解析成 dict 列表。"""
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# B1：格式 —— 一行一个 JSON 对象
# ---------------------------------------------------------------------------

def test_log_is_one_line_of_json(stream):
    LOGGER.info("task_started")

    raw = stream.getvalue().strip()
    assert "\n" not in raw                    # 单行是硬要求（grep / jq 的前提）
    assert json.loads(raw)["event"] == "task_started"


def test_fixed_fields_are_present(stream):
    LOGGER.info("task_started")

    event = events(stream)[0]
    assert event["level"] == "INFO"
    assert event["event"] == "task_started"
    assert event["logger"] == "app.test"
    # ts 要能被人和机器同时读懂：ISO8601 + UTC + 毫秒 + Z 结尾
    assert event["ts"].endswith("Z") and "T" in event["ts"]


def test_log_level_is_respected(stream):
    """级别过滤是日志系统的基本功能：DEBUG 不配就等于没有。"""
    LOGGER.debug("noise")
    LOGGER.warning("real_signal")

    assert [event["event"] for event in events(stream)] == ["real_signal"]


# ---------------------------------------------------------------------------
# B2：上下文 —— 绑一次，处处都有
# ---------------------------------------------------------------------------

def test_context_fields_are_injected_automatically(stream):
    """入口绑一次 task_id，任何模块、任何 logger 打出来的日志都自动带上它。"""
    with log_context(task_id="t-1", workflow_id="wf-1"):
        LOGGER.info("step_saved", extra={"iteration": 2})

    event = events(stream)[0]
    assert event["task_id"] == "t-1"
    assert event["workflow_id"] == "wf-1"
    assert event["iteration"] == 2             # 显式 extra 也在


def test_bind_context_merges_instead_of_replacing(stream):
    """后绑的字段是**合并**进去，不能把先绑的顶掉。"""
    bind_context(task_id="t-1")
    bind_context(iteration=3)
    LOGGER.info("step_saved")

    event = events(stream)[0]
    assert event["task_id"] == "t-1"
    assert event["iteration"] == 3


def test_explicit_extra_beats_context(stream):
    """显式 extra 优先级更高：调用点当场写死的，比环境上下文更具体、更权威。"""
    with log_context(task_id="from-context"):
        LOGGER.info("e", extra={"task_id": "from-extra"})

    assert events(stream)[0]["task_id"] == "from-extra"


def test_clear_context_removes_fields(stream):
    bind_context(task_id="t-1")
    clear_context()
    LOGGER.info("e")

    assert "task_id" not in events(stream)[0]


def test_log_context_restores_previous_context_on_exit(stream):
    """嵌套：内层退出后，外层绑的字段必须还在。

    "恢复原样"要用 set() 返回的 Token + reset(token)，
    而不是在 finally 里 clear_context()（那会把外层的字段一起清掉）。
    """
    with log_context(workflow_id="wf-1"):
        with log_context(task_id="t-1"):
            LOGGER.info("inner")
        LOGGER.info("outer")

    inner, outer = events(stream)
    assert (inner["task_id"], inner["workflow_id"]) == ("t-1", "wf-1")
    assert outer["workflow_id"] == "wf-1"
    assert "task_id" not in outer               # 内层绑的不能泄漏到外面


def test_current_context_returns_a_copy(stream):
    """调用方拿到的必须是副本，不能改到内部状态。"""
    bind_context(task_id="t-1")
    snapshot = current_context()
    snapshot["task_id"] = "tampered"

    assert current_context()["task_id"] == "t-1"


# ---------------------------------------------------------------------------
# B3：并发隔离 —— 本阶段的核心
# ---------------------------------------------------------------------------

def test_context_is_isolated_between_concurrent_tasks(stream):
    """并发的两个任务各带各的 task_id，绝不串号。

    这条测试就是"为什么不能用全局变量"的可执行证明：
    把 _context 换成一个模块级 dict，两个并发任务会互相覆盖，
    结果两个事件都写成后绑的那个 task_id —— 测试变红。

    await asyncio.sleep(0) 是故意的：让出控制权制造真实的交错
    （没有它，两个协程是顺序跑完的，全局变量的 bug 会被掩盖）。
    """

    async def job(task_id):
        bind_context(task_id=task_id)
        await asyncio.sleep(0)          # ← 让出控制权，暴露串号问题
        LOGGER.info("step", extra={"who": task_id})

    async def main():
        await asyncio.gather(job("task-a"), job("task-b"))

    asyncio.run(main())

    seen = {event["who"]: event["task_id"] for event in events(stream)}
    assert seen == {"task-a": "task-a", "task-b": "task-b"}
    # 协程里绑的字段不能泄漏回主上下文
    assert current_context() == {}


# ---------------------------------------------------------------------------
# B4：异常与验收标准
# ---------------------------------------------------------------------------

def test_exception_stays_on_one_line(stream):
    """堆栈必须被转义进 JSON，不能把一条事件拆成几十行。"""
    try:
        raise ValueError("boom")
    except ValueError:
        LOGGER.exception("task_failed")

    raw = stream.getvalue().strip()
    assert len(raw.splitlines()) == 1
    event = json.loads(raw)
    assert event["level"] == "ERROR"
    assert "ValueError: boom" in event["exc_info"]


def test_chinese_stays_readable_in_the_log_file(tmp_path):
    """中文不能变成 \\u5317\\u4eac。

    `json.dumps` 默认 `ensure_ascii=True`，会把所有非 ASCII 转成转义序列。
    后果是**日志的一切都还能跑**（JSON 合法、能解析、测试全绿），
    只有最常用的那个动作坏掉了：`grep 北京 app.log` 什么都查不到。

    观测系统的可用性，取决于最常用的那个查询能不能用。
    """
    log_path = tmp_path / "app.log"
    setup_logging(level="INFO", log_path=log_path)

    LOGGER.info("node_dispatched", extra={"node_id": "写最终报告"})

    text = log_path.read_text(encoding="utf-8")
    assert "写最终报告" in text          # 人能 grep 到
    assert "\\u" not in text             # 没有被转义成 \uXXXX


def test_grep_traces_one_task_through_the_log_file(tmp_path):
    """阶段 7 验收标准：`grep task_id 日志文件` 能串出该任务的完整执行史。

    这条测试模拟的是人的实际动作 —— 拿一个 task_id 去日志文件里查，
    按时间顺序还原"这个任务到底经历了什么"。
    """
    log_path = tmp_path / "app.log"
    setup_logging(level="INFO", log_path=log_path)

    with log_context(task_id="task-42", workflow_id="wf-1"):
        LOGGER.info("task_started")
        LOGGER.info("step_saved", extra={"iteration": 1})
        LOGGER.info("task_completed")
    LOGGER.info("workflow_finished", extra={"workflow_id": "wf-1"})   # 别的上下文

    text = log_path.read_text(encoding="utf-8")
    matched = [json.loads(line) for line in text.splitlines() if "task-42" in line]

    assert [event["event"] for event in matched] == [
        "task_started", "step_saved", "task_completed",
    ]
    assert matched[1]["iteration"] == 1
