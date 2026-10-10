import atexit
import json
import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from openai import OpenAI
from app.agent import AgentExecutor
from app.config import get_settings
from app.db import connect, get_db_path
from app.sse import event_stream
import os
from sse_starlette.sse import EventSourceResponse
from app.events import get_redis_client, publish_workflow_event, workflow_channel
from app.workflow_events import build_snapshot, is_terminal_workflow
from app.cancellation import cancel_task, TaskNotFoundError, TaskNotCancellableError, TERMINAL_STATUSES
from app.task_submit import create_and_enqueue_task
from app.planner import PlannerAgent
from app.workflow_store import SQLiteWorkflowStore
from app.trace import build_trace
from app.workflow_submit import create_workflow_from_goal
from app.approvals import (
    ApprovalAlreadyDecidedError,
    ApprovalNotFoundError,
    list_pending_approvals,
)
from app.approval_submit import decide_and_enqueue
from app.logging_setup import setup_logging
from app.queue import enqueue_task

load_dotenv()
# 进程入口配置一次日志（三个入口都要：API / orchestrator / worker）。
# 默认不落盘：容器里的最佳实践是"进程只写 stdout，由外部收集"；
# 本地排查时设 LOG_PATH=agent_uv.log 就能直接 grep。
setup_logging(log_path=get_settings().log_path)
logger = logging.getLogger(__name__)

# MCP 外部工具：进程启动时注册一次。fail-open —— 某个 server 起不来只是少一个工具，
# 平台照常启动；真正的调用失败由 bridge 的 handler 返回错误文本。
from app.mcp.bridge import register_mcp_tools, shutdown_mcp_clients  # noqa: E402

_mcp_tools = register_mcp_tools(get_settings().mcp_servers)
if _mcp_tools:
    logger.info("mcp_tools_registered", extra={"tools": _mcp_tools})
atexit.register(shutdown_mcp_clients)

app = FastAPI(title="Agent Platform")


def llm_client() -> OpenAI:
    """按当前配置建一个 LLM 客户端。

    抽成函数而不是每处各写一遍：**配置读取点越少，漏读的风险越小**
    （8-0 那个"API 写一个库、worker 读另一个库"的 bug 就是漏读造成的）。
    """
    settings = get_settings()
    return OpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url)


def llm_model() -> str:
    return get_settings().deepseek_model

class AgentRequest(BaseModel):
    question: str
    max_iterations: int = 5


class TaskSubmitRequest(BaseModel):
    question: str
    max_iterations: int = 5

@app.post("/agent/run")
def run_agent(request: AgentRequest):
    # 必须是普通 def：里面是整个 Agent 循环（同步阻塞，可能跑几分钟）。
    # 写成 async def 会把事件循环卡死（阶段 1 思考题 6）——
    # FastAPI 对 def 端点会自动丢到线程池执行。
    executor = AgentExecutor(
        llm_client=llm_client(), model=llm_model(), max_iterations=request.max_iterations
    )
    result = executor.run(request.question)
    return result

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/tasks", status_code=202)
async def submit_task(request: TaskSubmitRequest):
    """提交后台任务，立即返回 task_id。"""
    try:
        return create_and_enqueue_task(
            request.question,
            request.max_iterations,
            # ⚠️ 以前这里没传 db_path，函数默认写死库路径 ——
            # 只要 DATABASE_PATH 不是默认值，任务就写进另一个库，
            # 而 worker 去 DATABASE_PATH 里找 → "提交成功、返回 202、永远不执行、零报错"。
            db_path=get_db_path(),
            queue_client=get_redis_client(),
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    """返回任务详情以及按 iteration 排序的执行步骤。"""
    conn = connect(row_factory=True)
    try:
        task_row = conn.execute(
            "SELECT id, question, status, answer, max_iterations, created_at, updated_at "
            "FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task_row is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        step_rows = conn.execute(
            "SELECT id, iteration, type, tool_name, tool_args, observation, started_at, finished_at "
            "FROM steps WHERE task_id = ? ORDER BY iteration ASC", (task_id,)
        ).fetchall()
        steps = []
        for row in step_rows:
            item = dict(row)
            item["tool_args"] = json.loads(item["tool_args"]) if item["tool_args"] else None
            steps.append(item)
        return {"task": dict(task_row), "steps": steps}
    finally:
        conn.close()


@app.get("/tasks/{task_id}/events")
async def task_events(task_id: str):
    """通过 SSE 持续返回任务历史步骤和实时进度。"""
    conn = connect()
    try:
        exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    if exists is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    def load_history(loaded_task_id):
        conn = connect(row_factory=True)
        try:
            rows = conn.execute(
                "SELECT iteration, type, tool_name, tool_args, observation, started_at, finished_at "
                "FROM steps WHERE task_id = ? ORDER BY iteration ASC",
                (loaded_task_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def is_terminal(checked_task_id):
        conn = connect()
        try:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (checked_task_id,)
            ).fetchone()
            # 这里原本硬编码了一份终态列表，和 cancellation.TERMINAL_STATUSES
            # 是**同一份知识的两份拷贝** —— 加 BUDGET_EXCEEDED 时必然漏掉一处。
            # 改成 import TERMINAL_STATUSES 用同一份常量（同族问题：加状态要 grep 所有快照）。
            return row is None or row[0] in cancellation.TERMINAL_STATUSES
        finally:
            conn.close()

    redis_client = get_redis_client()
    def subscribe(channel):
        pubsub = redis_client.pubsub()
        pubsub.subscribe(channel)
        return pubsub
    

    return EventSourceResponse(event_stream(
        task_id,
        load_history=load_history, 
        subscribe=subscribe,
        is_terminal=is_terminal,
    ))


@app.get("/workflows/{workflow_id}/events")
async def workflow_events(workflow_id: str):
    """通过 SSE 推送 workflow 级进度：先发全量快照，再发实时事件。"""
    # 快照 = 从库里折叠出的"当前全量状态"；实时事件只是变更通知
    store = SQLiteWorkflowStore(get_db_path())
    workflow = store.get_workflow(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="workflow 不存在")
    
    def load_history(loaded_id):
        wf = store.get_workflow(loaded_id)
        tasks = store.list_node_tasks(loaded_id)  # 已按创建先后排序
        return build_snapshot(wf, tasks)  # 快照 = workflow 级"历史"
    
    def is_terminal(checked_id):
        wf = store.get_workflow(checked_id)
        return wf is None or is_terminal_workflow(wf["status"])
    
    def subscribe(channel):
        redis_client = get_redis_client()
        pubsub = redis_client.pubsub()
        pubsub.subscribe(channel)
        return pubsub
    
    return EventSourceResponse(event_stream(
        workflow_id,
        load_history=load_history,
        subscribe=subscribe,
        is_terminal=is_terminal,
        channel=workflow_channel(workflow_id)
    ))


@app.post("/tasks/{task_id}/cancel")
async def cancel_task_endpoint(task_id: str):
    """请求协作式取消任务。"""
    try:
        return cancel_task(task_id)
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="任务不存在")
    except TaskNotCancellableError:
        raise HTTPException(status_code=409, detail="任务已进入终态，不能取消")


class WorkflowSubmitRequest(BaseModel):
    goal: str
    # 阶段 7-A-2：提交时就能设预算（None = 不限量）。不设的话预算防线对这条 workflow
    # 就是关着的 —— 默认值选了 None 而不是某个数字，因为"悄悄给人一个上限"
    # 比"不设上限"更容易让人误判（钱花了但任务莫名失败）。
    budget_usd: float | None = None


@app.post("/workflows", status_code=202)
def submit_workflow(request: WorkflowSubmitRequest):
    # 同样必须是普通 def：Planner 的 LLM 调用是同步阻塞的（几秒）。
    # 高并发下 async def 会让所有请求排队等这一次规划（阶段 1 思考题 6 重现）。
    """提交复杂目标：Planner 拆成 DAG 后立即返回 workflow_id，由编排进程推进。"""
    planner = PlannerAgent(llm_client(), model=llm_model())
    store = SQLiteWorkflowStore(get_db_path())
    try:
        return create_workflow_from_goal(request.goal, planner, store,
                                         budget_usd=request.budget_usd)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))

class ApprovalDecisionRequest(BaseModel):
    approved: bool
    comment: str | None = None
    decided_by: str | None = None


@app.get("/approvals/pending")
def pending_approvals():
    """列出所有等待人工审批的节点（前端/审批人用）。

    用 def 而不是 async def：内部是阻塞的 sqlite 读，交给 FastAPI 线程池
    （和 /workflows、/agent/run 同一条规则）。
    """
    db_path = get_db_path()
    approvals = list_pending_approvals(db_path)
    return {"approvals": approvals}


@app.post("/approvals/{approval_id}/decide")
def decide_approval_endpoint(approval_id: str, decision: ApprovalDecisionRequest):
    """人类做出审批决定：批准 -> 任务重新入队；拒绝 -> 任务终止 + workflow 失败。"""
    try:
        # 注意 return：少了它接口会返回 null —— 单元测试抓不到，只有联调/前端能发现
        return decide_and_enqueue(
            approval_id,
            decision.approved,
            decision.comment,
            decision.decided_by,
            db_path=get_db_path(),
            enqueue=lambda task_id: enqueue_task(task_id, client=get_redis_client()),
        )
    except ApprovalNotFoundError:
        raise HTTPException(status_code=404, detail="审批不存在")
    except ApprovalAlreadyDecidedError:
        raise HTTPException(status_code=409, detail="审批已决定")


@app.get("/workflows/{workflow_id}/cost")
def workflow_cost_report(workflow_id: str):
    """这个 workflow 花了多少 token、多少钱（阶段 7-A）。"""
    # 已知缺口：**Planner 那次调用没有记账** —— 它不在 steps 表里，
    # 所以这里算的只是"执行阶段"的成本，不含规划成本。
    store = SQLiteWorkflowStore(get_db_path())
    workflow = store.get_workflow(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="workflow 不存在")
    model = llm_model()
    from app.cost import workflow_cost
    cost_report = workflow_cost(get_db_path(), workflow_id, model)
    return {
        "workflow_id": workflow_id,
        "model": model,
        "budget_usd": workflow["budget_usd"],
        **cost_report
    }


@app.get("/workflows/{workflow_id}/trace")
async def get_workflow_trace(workflow_id: str):
    """返回一条 workflow 的完整时间线（阶段 7-C）。

    回答三个 Metrics 答不了的问题：**哪一步最慢 / 哪一步 token 最多 / 关键路径是哪条**。
    "最慢"给两个数：duration_ms（墙钟，含工具和排队）和 llm_ms（纯模型延迟）——
    只看一个数字永远说不清该优化哪里。
    """
    store = SQLiteWorkflowStore(get_db_path())
    trace = build_trace(
        store, workflow_id, model=llm_model()
    )
    if trace is None:
        raise HTTPException(status_code=404, detail="workflow 不存在")
    return trace


@app.get("/workflows/{workflow_id}")
async def get_workflow_detail(workflow_id: str):
    """返回 workflow 详情 + 全部子任务（每个节点的状态和产出）。"""
    store = SQLiteWorkflowStore(get_db_path())
    workflow = store.get_workflow(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="workflow 不存在")
    return {"workflow": workflow, "tasks": store.list_node_tasks(workflow_id)}