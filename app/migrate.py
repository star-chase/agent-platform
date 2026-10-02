"""建表 / 迁移：作为**部署步骤**跑一次，不在应用启动时顺手做（阶段 8-A）。

    python -m app.migrate

## 为什么单独一个入口，而不是让每个进程启动时自己 ensure_schema

DDL 是"一次性、只有一个写者"的操作。让 api / worker / orchestrator 各自在启动时
跑一遍 `ensure_schema`，三个进程会**同时抢 DDL 锁**（PG 里 ALTER TABLE 要加表锁），
并发下互相等待，而且日志里会出现三份一模一样的迁移记录 ——
"谁建的这张表"变成一个没人答得上来的问题。

正确的形状是把它变成一个**显式的部署阶段**：先迁移，成功了再启动服务。
compose 里的 `depends_on: {condition: service_completed_successfully}` 就是干这个的 ——
它比普通的 `depends_on`（只等"启动顺序"）强得多，等的是"这个任务成功跑完了"。

## 为什么不用 Alembic

Alembic 解决的是"多人协作下线性的、可回滚的 schema 版本史"。本项目是一个人、
schema 已经稳定、迁移逻辑就是"缺列就加"（幂等），引进来是负担。
什么时候该换：有多人同时改 schema、或者需要生产环境的回滚能力时。
"""

import logging

from app.config import describe_settings, get_settings
from app.db import connect, ensure_schema
from app.logging_setup import setup_logging


# 和 worker / orchestrator 同款：`python -m` 启动时 __name__ 是 "__main__"，
# 日志里的 logger 字段会分不清是哪个进程。__spec__.name 永远是真实模块名。
logger = logging.getLogger(__spec__.name if __spec__ else __name__)


def main() -> None:
    settings = get_settings()
    setup_logging(log_path=settings.log_path)
    logger.info("migration_starting", extra=describe_settings(settings))

    conn = connect()
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    logger.info("migration_finished", extra={"dialect": settings.dialect})


if __name__ == "__main__":
    main()
