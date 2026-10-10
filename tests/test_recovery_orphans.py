"""孤儿 PENDING 任务回收（故障注入实测后补的补偿链路）。

场景（2026-10-10 实测）：Redis 被 kill 掉，队列里的 task_id 全没了；但 tasks 表里这些任务
还是 `PENDING`，node 也已经在 `dispatched` 集合里 —— 调度器不会再派发它们，
流水线永久停在 `RUNNING`。只回收卡在 `RUNNING` 的任务（`recover_stale_tasks`）覆盖不到这条路径。

本文件锁定四条契约：
  1. 够老的 PENDING 任务会被重新入队；
  2. 刚创建的 PENDING 任务不动（否则会和正在抢它的 worker 撞车）；
  3. 非 PENDING（RUNNING / COMPLETED）一个都不碰；
  4. 开关为 0（默认）时整个功能关闭 —— 改变默认行为要显式配置。
"""

from app.db import connect, ensure_schema
from app.recovery import recover_orphan_pending_tasks
from app.workflow_store import SQLiteWorkflowStore


def make_store(tmp_path, name="orphans.db"):
    db_path = str(tmp_path / name)
    conn = connect(db_path)
    ensure_schema(conn)
    conn.close()
    return SQLiteWorkflowStore(db_path), db_path


def make_task(store, db_path, workflow_id, node_id, age_seconds=None, status=None):
    task_id = store.create_node_task(workflow_id, node_id, "q", [])
    conn = connect(db_path)
    try:
        if age_seconds is not None:
            conn.execute(
                "UPDATE tasks SET updated_at = datetime('now', ?) WHERE id = ?",
                (f"-{age_seconds} seconds", task_id),
            )
        if status is not None:
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        conn.commit()
    finally:
        conn.close()
    return task_id


def test_orphan_pending_task_is_requeued(tmp_path):
    store, db_path = make_store(tmp_path)
    workflow_id = store.create_workflow("目标", {"nodes": [{"id": "A"}]}, status="RUNNING")
    task_id = make_task(store, db_path, workflow_id, "A", age_seconds=120)
    enqueued: list[str] = []

    recovered = recover_orphan_pending_tasks(
        db_path, older_than_seconds=30, enqueue=enqueued.append
    )

    assert recovered == [task_id]
    assert enqueued == [task_id]


def test_fresh_pending_task_is_not_requeued(tmp_path):
    store, db_path = make_store(tmp_path)
    workflow_id = store.create_workflow("目标", {"nodes": [{"id": "A"}]}, status="RUNNING")
    make_task(store, db_path, workflow_id, "A")
    enqueued: list[str] = []

    assert recover_orphan_pending_tasks(db_path, older_than_seconds=30, enqueue=enqueued.append) == []
    assert enqueued == []


def test_running_and_completed_tasks_are_untouched(tmp_path):
    store, db_path = make_store(tmp_path)
    workflow_id = store.create_workflow("目标", {"nodes": [{"id": "A"}, {"id": "B"}]}, status="RUNNING")
    make_task(store, db_path, workflow_id, "A", age_seconds=600, status="RUNNING")
    make_task(store, db_path, workflow_id, "B", age_seconds=600, status="COMPLETED")
    enqueued: list[str] = []

    assert recover_orphan_pending_tasks(db_path, older_than_seconds=30, enqueue=enqueued.append) == []
    assert enqueued == []


def test_disabled_when_threshold_is_zero(tmp_path):
    store, db_path = make_store(tmp_path)
    workflow_id = store.create_workflow("目标", {"nodes": [{"id": "A"}]}, status="RUNNING")
    make_task(store, db_path, workflow_id, "A", age_seconds=600)
    enqueued: list[str] = []

    assert recover_orphan_pending_tasks(db_path, older_than_seconds=0, enqueue=enqueued.append) == []
    assert enqueued == []


def test_requeue_keeps_status_pending(tmp_path):
    """重新入队不动任务状态：重复消费由 worker 的"先查状态"兜住，不靠这里加锁。"""
    store, db_path = make_store(tmp_path)
    workflow_id = store.create_workflow("目标", {"nodes": [{"id": "A"}]}, status="RUNNING")
    task_id = make_task(store, db_path, workflow_id, "A", age_seconds=120)
    recover_orphan_pending_tasks(db_path, older_than_seconds=30, enqueue=lambda _task_id: None)

    conn = connect(db_path)
    try:
        status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
    finally:
        conn.close()
    assert status == "PENDING"


def test_second_call_within_threshold_is_noop(tmp_path):
    """补偿一次之后把 updated_at 推到当前时间：同一批任务不会每个 tick 被反复投递。"""
    store, db_path = make_store(tmp_path)
    workflow_id = store.create_workflow("目标", {"nodes": [{"id": "A"}]}, status="RUNNING")
    make_task(store, db_path, workflow_id, "A", age_seconds=120)
    enqueued: list[str] = []

    first = recover_orphan_pending_tasks(db_path, older_than_seconds=30, enqueue=enqueued.append)
    second = recover_orphan_pending_tasks(db_path, older_than_seconds=30, enqueue=enqueued.append)

    assert len(first) == 1
    assert second == []
    assert len(enqueued) == 1
