"""结构化日志（阶段 7-B）：可观测性三支柱的第二根。

## 为什么 print 不够

项目里现在散着几句 `print("📍 从第 3 轮继续…")`。人读得懂，机器读不懂：

    print(f"任务 {task_id} 失败")            → 想查"哪些任务失败过"，只能写正则
    logger.error("task_failed", task_id=..)  → jq 'select(.event=="task_failed")'

日志的消费者**不是人，是 grep / jq / ELK / Loki**。给人看的那部分是"渲染"，
不是"存储格式"。所以存储格式必须是结构化的（一行一个 JSON 对象）。

## 为什么用 contextvars，而不是全局变量 / 层层传参

一个 task_id 要穿过 HTTP → 队列 → worker → 执行器 → 工具，中间十几层函数：

    层层传参  → 每个函数签名都要加 task_id，且和业务参数混在一起（"参数穿透"）
    全局变量  → 并发时**串号**：worker 同时跑两个任务，后写的覆盖先写的
    contextvars → 并发安全 + 零参数穿透：入口绑一次，下面所有日志自动带上

contextvars 是 asyncio 原生的"任务局部变量"：每个 asyncio Task（以及每个线程）
拿到的是自己那份拷贝，写互不影响。这正是它在标准库里的存在理由，
也是本模块唯一需要"理解"而非"记忆"的知识点。

## 一份日志长什么样

    {"ts":"2026-09-21T12:00:00.123Z","level":"INFO","event":"step_saved",
     "logger":"app.agent_fc_persistent","task_id":"t-1","workflow_id":"wf-1","iteration":2}

固定字段：`ts` / `level` / `event` / `logger`；其余全是上下文或显式 extra。
"""

from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Iterator

# LogRecord 自带属性：它们**不是**业务字段，不能被打进 JSON。
# 这就是为什么"额外字段"要用 `record.__dict__` 减去这份白名单 ---
# logging 模块没有提供"只给我 extra"的 API，这是个众所周知的缺口。
RESERVED_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
})

# 当前请求/任务的日志上下文（阶段 7-B）。default 用 {} 而不是 None：省掉每处的空判断。
_context: ContextVar[dict[str, Any]] = ContextVar("log_context", default={})


def bind_context(**fields: Any) -> None:
    """把字段**合并**进当前上下文。

    合并（而不是替换）：入口绑 task_id、执行器再绑 iteration 时，
    task_id 不能让后者顶掉。

    ⚠️ 不要原地改 `_context.get()` 那个 dict —— 当调用处于 default 值分支时，
    那个 dict 是所有上下文共享的同一个对象，原地改会**跨任务泄漏**。
    正确做法是造一个新 dict 再 set 进去。
    """
    old = _context.get()
    new = {**old, **fields}
    _context.set(new)


def clear_context() -> None:
    """清空当前上下文。

    为什么必须清：worker 是长期运行的进程，处理完任务 A 再处理任务 B 时，
    如果不清，B 的日志会带上 A 的 task_id —— 串号比没有上下文更糟
    （你会照着一份错误的执行史去排查问题）。
    """
    _context.set({})


def current_context() -> dict[str, Any]:
    """返回当前上下文的**副本**。

    必须是副本：调用方拿到内部那个 dict 就能改到上下文，
    隐式状态被外部改掉是最难查的一类 bug。
    （`dict(...)` 是浅拷贝，对本项目这种扁平结构足够了。）
    """
    return dict(_context.get())


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """with 块内绑定字段，退出时**恢复原样**。

    用法（入口处绑一次，往下十几层都不用再管）：

        with log_context(task_id=task_id, workflow_id=workflow_id):
            run_task(task)          # 里面所有日志自动带 task_id

    实现要点：`ContextVar.set()` 会返回一个 Token，`reset(token)` 才是
    "恢复到 set 之前"，而不是"设回空"。两者在嵌套使用时差别很大：
    外层绑了 workflow_id，内层只绑 task_id，内层退出后 workflow_id 必须还在。
    """
    old = _context.get()
    new = {**old, **fields}
    token = _context.set(new)
    try:
        yield
    finally:
        _context.reset(token)


class JsonFormatter(logging.Formatter):
    """把一条 LogRecord 渲染成**单行** JSON。

    单行是硬要求：整个日志分析链（grep / jq / Filebeat）都假设"一行一条事件"。
    多行堆栈会把一条事件拆成几十行，一行一条的假设当场破产。
    json.dumps 会自动把字符串里的换行转义成 \\n，所以只要不手动拼换行就安全。
    """

    def format(self, record: logging.LogRecord) -> str:
        """返回一个 JSON 字符串。

        契约（tests/test_logging.py 逐条断言了）：

        1. 固定字段：
             ts     —— `2026-09-21T12:00:00.123Z` 形状（UTC + 毫秒 + Z 结尾）
                       一行到手：
                       datetime.fromtimestamp(record.created, tz=timezone.utc)
                               .isoformat(timespec="milliseconds").replace("+00:00", "Z")
             level  —— record.levelname
             event  —— record.getMessage()（已经做过 %-格式化；**别用 record.msg**，
                       那是模板，参数还在 record.args 里）
             logger —— record.name

        2. 业务字段 = contextvars 的字段，**再**盖上一层显式 extra。
           优先级是 **extra > context**：extra 是调用点当场写死的、更具体
           （`logger.info("e", extra={"task_id": ...})` 明显比环境上下文更权威）。

        3. 显式 extra 怎么取：`record.__dict__` 里不在 RESERVED_ATTRS 的键。

        4. 有异常时加 `exc_info` 字段：`self.formatException(record.exc_info)`
           （返回多行字符串 —— 交给 json.dumps 转义即可，别自己处理）。

        5. `json.dumps(..., ensure_ascii=False)`：中文日志不要变成 \\uXXXX，
           否则 grep 一个中文关键词会永远查不到。

        提示：先 `payload = {}`，按"固定字段 → context → extra → exc_info"
        的顺序往里塞，最后 dumps。顺序就是优先级，先塞的会被后塞的覆盖。
        """
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
        }
        payload.update(_context.get())
        payload.update({
            k: v for k, v in record.__dict__.items() if k not in RESERVED_ATTRS
        })
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", stream=None, log_path=None) -> logging.Handler:
    """配置**根 logger**：所有 `logging.getLogger(__name__)` 的输出都走 JSON 一行。

    这是基础设施（已经给好了），不用你写。三个设计点：
      - 幂等：先清掉已有 handler。测试里会反复调用，不清就会叠加 N 个 handler、
        同一条日志被打印 N 遍（然后你的断言看到重复行，一脸问号）。
      - 配根 logger 而不是逐个配业务 logger：`getLogger("app.worker")` 会向上冒泡，
        一处配置全项目生效 —— 漏配某个模块的日志级别是常见的"日志不见了"原因。
      - log_path 优先于 stream：验收标准里要 `grep task_id 日志文件`。
    """
    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    # 用真值判断而不是 `is not None`：空字符串在这里是"没配"，
    # 传进去会让 FileHandler 去打开一个空路径（结果是当前目录，直接 IsADirectoryError）。
    if log_path:
        handler: logging.Handler = logging.FileHandler(log_path, encoding="utf-8")
    else:
        handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    return handler
