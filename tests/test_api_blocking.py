"""API 端点的阻塞语义（阶段 1 思考题 6 的回归防线）。

FastAPI 的规则很简单：
    async def 端点  -> 直接跑在事件循环里，里面**绝不能有阻塞调用**
    def 端点        -> FastAPI 自动丢到线程池，阻塞也不影响其他请求

我们的 /agent/run 和 /workflows 内部是同步阻塞的 LLM 调用（几秒到几分钟），
所以必须是普通 def。这条规则不写测试钉住，下次很容易又写成 async def。
"""

import inspect

from app.main import run_agent, submit_workflow, task_events, workflow_events


def test_llm_endpoints_are_sync_functions():
    """带同步 LLM 调用的端点必须是 def，交给 FastAPI 的线程池。"""
    assert not inspect.iscoroutinefunction(run_agent), (
        "/agent/run 里有整个 Agent 循环（阻塞），不能写成 async def"
    )
    assert not inspect.iscoroutinefunction(submit_workflow), (
        "/workflows 里有 Planner 的同步 LLM 调用，不能写成 async def"
    )


def test_sse_endpoints_stay_async():
    """SSE 是流式响应，本来就是 await 驱动的，必须保持 async。"""
    assert inspect.iscoroutinefunction(task_events)
    assert inspect.iscoroutinefunction(workflow_events)
