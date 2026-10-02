"""数据库访问层 —— 全项目**唯一**的连接入口，同时支持 SQLite 与 PostgreSQL（阶段 8-0）。

## 为什么要有这一层

阶段 3 建它时只为了解决 SQLite 的并发写锁（busy_timeout + WAL）。阶段 8-0 换 PostgreSQL
时它的价值才真正兑现：**两个方言的差异全部收在这一个文件里**，业务代码一行都不用改。

原始统计（迁移前的"方言污染面"）：5 个应用文件 `import sqlite3`、36 处 `?` 占位符、
15 处 `datetime('now')`、6 处 `PRAGMA`、2 处 `ORDER BY rowid`。如果当时没有这一层，
这些全都得在业务代码里一个个改，而且改完就再也没法跑 SQLite 测试了。

## 方言差异只有四类（这就是全部）

| 差异 | SQLite | PostgreSQL | 本文件怎么抹平 |
|---|---|---|---|
| 占位符 | `?` | `%s` | `_translate()` 改写 |
| 当前时间 | `datetime('now')` | `NOW()` | `_translate()` 改写 |
| 自增序号 | 隐式 `rowid` | 显式 `seq` 列 | `_translate()` 把 `rowid` 改写成 `seq` |
| 建表/迁移 DDL | `PRAGMA table_info` | `ADD COLUMN IF NOT EXISTS` | `ensure_schema()` 分方言 |

**只有 DDL 必须分家**（两边的类型系统和迁移语法是两套东西），
**DML 一律走改写** —— 业务 SQL 保持 SQLite 风格可读可测。

## 怎么切换

    DATABASE_URL=postgresql://agent:agent123@postgres:5432/agent_platform  → PostgreSQL
    DATABASE_PATH=agent.db（或不设）                                        → SQLite

SQLite 路径**故意保留**：230 条离线测试跑在临时 SQLite 上只要 1 秒、零外部依赖。
"能换库"不等于"必须换库"—— 测试用 SQLite、生产用 PG，是这个项目最划算的组合。
"""

import os
import sqlite3
from datetime import datetime, timezone

from app.config import get_settings

DEFAULT_DB_PATH = "agent.db"
SQLITE_TIMEOUT_SECONDS = 30

def utc_now_ms() -> str:
    """当前 UTC 时间，**毫秒精度**，字符串格式与 SQLite 的 datetime('now') 完全一致。

    为什么要毫秒（阶段 7-C）：`datetime('now')` 只到秒。一次 LLM 调用常常 1~3 秒，
    用秒级时间戳算出来的耗时不是 0 就是 1，**整条 trace 的精度被这一个函数吃掉**。
    格式仍是 `'YYYY-MM-DD HH:MM:SS.mmm'`（空格分隔、UTC、无时区后缀），
    和库里既有的 `created_at` / `updated_at` 同族，`datetime.fromisoformat` 能直接解析。

    时间统一由**一处**产出（这里），不要在业务代码里各写各的 ——
    7-A 加 `decided_at` 时就踩过：本地时区、格式、精度各写各的，几种格式混在一张表里。

    为什么用应用侧时钟而不是 `strftime(...,'now')`：一条 trace 里的时间戳必须**同源** ——
    started_at 和 finished_at 都来自应用进程，相减才有意义。混用"应用取开始、
    数据库取结束"会把两者之间的时钟漂移算进耗时里（阶段 8-0 换远程 PG 后这个问题会放大）。
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def get_db_path() -> str:
    """当前进程该用哪个库（阶段 8-B 起统一由 app.config 提供）。

    **全项目唯一的库路径来源。** 以前每个文件各写一遍
    `os.getenv("DATABASE_PATH", "agent.db")`，而 `app/main.py` 的单 Agent 端点
    干脆写死 `sqlite3.connect("agent.db")` —— 于是只要 DATABASE_PATH 不是默认值，
    API 写一个库、worker 读另一个库：任务提交返回 202，然后永远不执行、零报错
    （2026-10-01 真实踩到，单测全绿）。默认值重复 N 遍 = 漏改 N-1 次。

    做成函数而不是模块级常量：`connect()` 也是每次调用现读环境变量，
    保持"改了环境变量立刻生效"的语义一致。
    """
    return get_settings().database_path


def get_database_url() -> str | None:
    """PostgreSQL 连接串。设了它就用 PG，否则回落到 SQLite 文件。"""
    return get_settings().database_url


def current_dialect() -> str:
    """当前该走哪个方言：`postgres` / `sqlite`。

    判断只看 DATABASE_URL 的 scheme —— 不猜、不试探连接。
    配置决定行为，行为可预测（测试里 monkeypatch 环境变量就能切换）。
    """
    return get_settings().dialect


class Row:
    """一行结果，语义**对齐 sqlite3.Row**。

    为什么要自己写一个：两个驱动的行对象不一样（sqlite3 默认给元组，psycopg 默认也给元组，
    但 `dict_row` 给字典），而业务代码**两种用法都有**：

        question, status, max_iterations = task_row     ← 当序列解包
        dict(task_row) / task_row["answer"] / task_row[0] ← 当字典或序列取值

    dict 子类会在第一行上炸（解包一个 dict 得到的是 key）。所以这里实现成
    "既能按下标取、又能按列名取、迭代时给值" —— 和 sqlite3.Row 一模一样。
    """

    __slots__ = ("_values", "_index")

    def __init__(self, columns: list[str], values) -> None:
        self._values = tuple(values)
        self._index = {name: position for position, name in enumerate(columns)}

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._values[self._index[key]]

    def __iter__(self):
        return iter(self._values)          # 解包 / tuple(row) / list(row) 都给"值"

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> list[str]:
        return list(self._index)           # dict(row) 靠 keys() + __getitem__ 工作

    def __repr__(self) -> str:
        pairs = ", ".join(f"{name}={value!r}" for name, value in zip(self._index, self._values))
        return f"Row({pairs})"


def _sqlite_row_factory(cursor, values) -> Row:
    return Row([column[0] for column in cursor.description], values)


def _postgres_row_factory(cursor):
    """psycopg 的 row_factory 是"先给一个 cursor、返回一个处理 values 的函数"。

    坑：psycopg 对 **CREATE TABLE / ALTER TABLE 这类没有结果集的语句也会调用它**，
    此时 `cursor.description` 是 None —— 直接迭代就 TypeError。
    sqlite3 的 row_factory 只在真的取行时才调用，两个驱动在这里行为不同，
    这类边角差异正是适配层存在的理由。
    """
    description = cursor.description
    columns = [column.name for column in description] if description else []

    def make(values) -> Row:
        return Row(columns, values)

    return make


# PostgreSQL 侧统一用这个表达式产出时间戳：格式和 SQLite 的 datetime('now')
# **逐字符一致**（'YYYY-MM-DD HH:MM:SS.mmm'，UTC）。
#
# 为什么必须精确一致（真实踩到过）：一开始这里写的是 `NOW()`，PG 把 timestamptz 转成
# 文本时给出 '2026-10-01 08:34:07.346574+00' —— 微秒 + 时区后缀，和 SQLite 的
# 'YYYY-MM-DD HH:MM:SS.mmm' 完全是两种字符串。后果不只是难看：
# `duration_ms` 相减时，一个带时区一个不带，Python 直接 TypeError
# （can't subtract offset-naive and offset-aware datetimes）—— **trace 端点在 PG 上必崩**。
# 单测跑在 SQLite 上，永远抓不到这种"只在另一个方言上成立"的 bug。
_PG_NOW_TEXT = "to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.MS')"

# PostgreSQL 的 SQL 改写表。**方言差异就这几条**，加新差异请加在这里，不要散到业务代码里。
_POSTGRES_REWRITES = (
    # ⚠️ 带修饰符的形式必须排在前面：`datetime('now', '-15 minutes')` 长得像上面那个函数，
    # 其实是**另一个 API**（SQLite 的日期修饰符），PG 里没有对应物。
    # 这就是"grep 出 15 处 datetime('now')"不足以摸清方言污染面的原因：
    # 数量好数，**变体难找**。真找齐它们只能靠"把两个方言都跑一遍"。
    ("datetime('now', ?)",
     "to_char(NOW() AT TIME ZONE 'UTC' + ?::interval, 'YYYY-MM-DD HH24:MI:SS.MS')"),
    ("datetime('now')", _PG_NOW_TEXT),
    ("rowid", "seq"),          # SQLite 的隐式自增序号，PG 侧靠显式 seq 列（见 ensure_schema）
)


# SQLite 侧也要改写一处：`datetime('now')` **只精确到秒**，而 PG 侧我们用的是毫秒。
# 不改的话两个方言的时间戳长度都不一样（19 字符 vs 23 字符），
# 字符串比较、排序、前端展示又得各写一套 —— 一个方言差异没抹平，就会长出一堆新分支。
_SQLITE_REWRITES = (
    ("datetime('now', ?)", "strftime('%Y-%m-%d %H:%M:%f', 'now', ?)"),
    ("datetime('now')", "strftime('%Y-%m-%d %H:%M:%f', 'now')"),
)


def _translate(sql: str) -> str:
    """把"统一写法"的 SQL 改写成当前方言。

    业务代码一律写 SQLite 风格的 `?` 占位符和 `datetime('now')`，
    到了这里才变成目标方言 —— 这样业务 SQL 保持可读、可离线测试。
    """
    if current_dialect() == "postgres":
        for old, new in _POSTGRES_REWRITES:
            sql = sql.replace(old, new)
        return sql.replace("?", "%s")
    for old, new in _SQLITE_REWRITES:
        sql = sql.replace(old, new)
    return sql


class Cursor:
    """把两个驱动的 cursor 归一化：fetchone/fetchall 统一返回 Row。"""

    def __init__(self, raw) -> None:
        self._raw = raw

    def fetchone(self) -> Row | None:
        values = self._raw.fetchone()
        if values is None or self._raw.description is None:
            return None
        if isinstance(values, Row):
            return values
        return Row([column[0] for column in self._raw.description], values)

    def fetchall(self) -> list[Row]:
        values = self._raw.fetchall()
        if self._raw.description is None:
            return []
        return [self._wrap(row) for row in values]

    def _wrap(self, values) -> Row:
        if isinstance(values, Row):
            return values
        return Row([column[0] for column in self._raw.description], values)

    def __iter__(self):
        """支持 `for row in conn.execute(...)`。"""
        return iter(self.fetchall())

    @property
    def rowcount(self) -> int:
        return self._raw.rowcount


class _NamedCursor:
    """兼容 `cursor = conn.cursor(); cursor.execute(...)` 这种老写法。

    项目的 SQL 有两种写法：`conn.execute(...)` 和 `cursor = conn.cursor()` 之后再用。
    换库时把后者全改掉是一次纯粹的重写（几十处、零收益），不如让适配层多吃一种写法。
    **适配层的价值就是"让业务代码不用改"**，不是"让业务代码变得整齐"。
    """

    def __init__(self, connection: "Connection") -> None:
        self._connection = connection
        self._cursor: Cursor | None = None

    def execute(self, sql: str, params=()) -> "Cursor":
        self._cursor = self._connection.execute(sql, params)
        return self._cursor

    def fetchone(self):
        return self._cursor.fetchone() if self._cursor else None

    def fetchall(self):
        return self._cursor.fetchall() if self._cursor else []

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount if self._cursor else -1


class Connection:
    """统一连接接口：execute / cursor / commit / close。

    业务代码只认识这三个方法，所以它完全不知道自己跑在 SQLite 还是 PostgreSQL 上。
    """

    def __init__(self, raw, dialect: str) -> None:
        self._raw = raw
        self.dialect = dialect

    def execute(self, sql: str, params=()) -> Cursor:
        cursor = self._raw.cursor()
        cursor.execute(_translate(sql), tuple(params))
        return Cursor(cursor)

    def cursor(self) -> _NamedCursor:
        return _NamedCursor(self)

    def executemany(self, sql: str, param_rows) -> Cursor:
        """批量执行。参数逐行翻译（SQL 改写在 execute 里做，这里复用同一份改写）。"""
        cursor = self._raw.cursor()
        cursor.executemany(_translate(sql), [tuple(row) for row in param_rows])
        return Cursor(cursor)

    def commit(self) -> None:
        self._raw.commit()

    def close(self) -> None:
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def connect(db_path: str | None = None, *, row_factory: bool = False) -> Connection:
    """建立连接。DATABASE_URL 有值就走 PostgreSQL，否则用 SQLite 文件。

    `row_factory` 保留只是为了不改调用方 —— 现在**两种方言都返回 Row**
    （Row 同时支持下标和列名），所以这个参数已经没有作用了。
    """
    if current_dialect() == "postgres":
        import psycopg

        raw = psycopg.connect(get_database_url(), row_factory=_postgres_row_factory)
        return Connection(raw, "postgres")

    raw = sqlite3.connect(
        db_path or get_db_path(),
        timeout=SQLITE_TIMEOUT_SECONDS,
    )
    # 让 sqlite3 也返回 Row：和 PG 侧行为一致，业务代码才不用分方言
    raw.row_factory = _sqlite_row_factory
    raw.execute(f"PRAGMA busy_timeout = {SQLITE_TIMEOUT_SECONDS * 1000}")
    raw.execute("PRAGMA journal_mode = WAL")
    return Connection(raw, "sqlite")


def ensure_schema(conn: Connection) -> None:
    """幂等地建齐所有表并补齐后加的列。两个方言各一套 DDL。

    为什么只有 DDL 必须分家：类型系统、迁移语法、`rowid` 的有无都是两套东西；
    而 DML（增删改查）的差异只有占位符/时间函数，`_translate()` 就够了。
    **分支越少，越不容易漏改** —— 这里只分一次。
    """
    # getattr 兜底：测试里有些地方直接传原始的 sqlite3 连接（它们只是要个临时库），
    # 让适配层容忍这一点，比逼着 9 个测试文件改写法划算。
    if getattr(conn, "dialect", "sqlite") == "postgres":
        _ensure_schema_postgres(conn)
    else:
        _ensure_schema_sqlite(conn)
    conn.commit()


def _ensure_schema_sqlite(conn: Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            question TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            answer TEXT,
            max_iterations INTEGER DEFAULT 5,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            started_at TEXT,          -- 阶段 7-C：worker 真正开始跑它的时刻
            finished_at TEXT          -- 阶段 7-C：跑完（或失败）的时刻
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS steps (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            iteration INTEGER NOT NULL,
            type TEXT NOT NULL,
            tool_name TEXT,
            tool_args TEXT,
            observation TEXT,
            started_at TEXT DEFAULT (datetime('now')),
            finished_at TEXT,
            llm_message_json TEXT,
            FOREIGN KEY (task_id) REFERENCES tasks(id),
            UNIQUE (task_id, iteration)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workflows (
            id TEXT PRIMARY KEY,
            goal TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PLANNING',
            dag TEXT NOT NULL,
            final_output TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            workflow_id TEXT,
            node_id TEXT,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            comment TEXT,
            decided_by TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            decided_at TEXT,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
        """
    )

    # 后加的列：SQLite 没有 ADD COLUMN IF NOT EXISTS，只能先查 PRAGMA 再决定加不加
    task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    for column, ddl in (
        ("workflow_id", "TEXT"),
        ("node_id", "TEXT"),
        ("depends_on", "TEXT DEFAULT '[]'"),
        ("upstream_outputs", "TEXT"),
        ("started_at", "TEXT"),
        ("finished_at", "TEXT"),
    ):
        if column not in task_columns:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {ddl}")

    step_columns = {row[1] for row in conn.execute("PRAGMA table_info(steps)").fetchall()}
    if "llm_message_json" not in step_columns:
        conn.execute("ALTER TABLE steps ADD COLUMN llm_message_json TEXT")
    for column, ddl in (
        ("input_tokens", "INTEGER DEFAULT 0"),
        ("output_tokens", "INTEGER DEFAULT 0"),
    ):
        if column not in step_columns:
            conn.execute(f"ALTER TABLE steps ADD COLUMN {column} {ddl}")

    workflow_columns = {row[1] for row in conn.execute("PRAGMA table_info(workflows)").fetchall()}
    if "budget_usd" not in workflow_columns:
        conn.execute("ALTER TABLE workflows ADD COLUMN budget_usd REAL")


def _ensure_schema_postgres(conn: Connection) -> None:
    """PostgreSQL 版建表。

    两个关键差异：
    1. **`tasks.seq` 显式自增列**：SQLite 有隐式的 `rowid`，PG 没有。
       而"节点级重试按插入顺序判定最新一次尝试"（5-G 的核心不变式）**依赖插入顺序**，
       所以必须把它变成显式的一等公民。`_translate()` 负责把 SQL 里的 rowid 改写成 seq。
    2. `ADD COLUMN IF NOT EXISTS` 是 PG 原生语法 —— 迁移逻辑比 SQLite 侧更短，
       不用先查 PRAGMA 再决定（阶段 3 那套 try/except duplicate column 也可以退休了）。
    """
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            seq BIGINT GENERATED ALWAYS AS IDENTITY,
            question TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            answer TEXT,
            max_iterations INTEGER DEFAULT 5,
            created_at TEXT DEFAULT {_PG_NOW_TEXT},
            updated_at TEXT DEFAULT {_PG_NOW_TEXT},
            started_at TEXT,
            finished_at TEXT
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS steps (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            iteration INTEGER NOT NULL,
            type TEXT NOT NULL,
            tool_name TEXT,
            tool_args TEXT,
            observation TEXT,
            started_at TEXT DEFAULT {_PG_NOW_TEXT},
            finished_at TEXT,
            llm_message_json TEXT,
            UNIQUE (task_id, iteration)
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS workflows (
            id TEXT PRIMARY KEY,
            goal TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PLANNING',
            dag TEXT NOT NULL,
            final_output TEXT,
            created_at TEXT DEFAULT {_PG_NOW_TEXT},
            updated_at TEXT DEFAULT {_PG_NOW_TEXT}
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            workflow_id TEXT,
            node_id TEXT,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            comment TEXT,
            decided_by TEXT,
            created_at TEXT DEFAULT {_PG_NOW_TEXT},
            decided_at TEXT
        )
        """
    )

    # PG 原生支持 IF NOT EXISTS —— 不需要先查 information_schema 再决定
    for column, ddl in (
        ("workflow_id", "TEXT"),
        ("node_id", "TEXT"),
        ("depends_on", "TEXT DEFAULT '[]'"),
        ("upstream_outputs", "TEXT"),
        ("started_at", "TEXT"),
        ("finished_at", "TEXT"),
        ("budget_usd", "REAL"),      # 注意：budget_usd 属于 workflows，见下面单独一条
    ):
        if column == "budget_usd":
            continue
        conn.execute(f"ALTER TABLE tasks ADD COLUMN IF NOT EXISTS {column} {ddl}")

    conn.execute("ALTER TABLE steps ADD COLUMN IF NOT EXISTS llm_message_json TEXT")
    conn.execute("ALTER TABLE steps ADD COLUMN IF NOT EXISTS input_tokens INTEGER DEFAULT 0")
    conn.execute("ALTER TABLE steps ADD COLUMN IF NOT EXISTS output_tokens INTEGER DEFAULT 0")
    conn.execute("ALTER TABLE workflows ADD COLUMN IF NOT EXISTS budget_usd REAL")
