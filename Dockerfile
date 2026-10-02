# 多阶段构建：构建期的编译工具不进最终镜像。
# python:3.14-slim 而不是 alpine —— 本项目的依赖里 openai/psycopg 都有 manylinux 预编译包，
# 用 slim(glibc) 能直接装 wheel；alpine 是 musl，很多包没有 wheel，要现场编译（慢且容易失败）。
FROM python:3.14-slim

# PYTHONUNBUFFERED：不加的话 stdout 会被缓冲，`docker logs` 里看不到实时日志 ——
# 日志的服务对象是排障的人，看不到等于没有。
# PYTHONDONTWRITEBYTECODE：容器里的 .pyc 没有复用价值，只会让镜像变大。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖单独一层，**放在 COPY 代码之前**：只要 requirements.txt 没变，
# 改业务代码时这一层直接命中缓存 —— 构建从几分钟降到几秒。
# 这是 Dockerfile 里收益最高的一条排序规则。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --shell /usr/sbin/nologin appuser

# ⚠️ 必须 --chown：COPY 出来的文件属主是 root，而**权限位是从构建上下文原样带过来的**。
# 本机源码恰好是 600（只有属主可读），于是 USER appuser 之后
# 连 `import app.db` 都读不了 —— 报的是 `PermissionError: /app/app/db.py`。
# 这个坑在本地怎么都复现不了（本地就是那个属主），只在容器里炸。
# 两条一起给：属主换成 appuser + 权限兜底可读。
COPY --chown=appuser:appuser app/ ./app/

# 不以 root 跑应用：容器逃逸时拿到的权限小得多。
# 顺带解决一个实际问题：root 写的文件属主是 root，挂载卷之后宿主机改不动。
USER appuser

# 默认跑 worker；api / orchestrator 由 compose 用 command 覆盖。
# 三个进程共用同一个镜像、不同的启动命令 —— 部署单元是"镜像 + 命令"，不是三个镜像。
CMD ["python", "-m", "app.worker"]
