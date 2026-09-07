import json
import sqlite3
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from openai import OpenAI
from app.agent import AgentExecutor
import os

load_dotenv()
app = FastAPI(title="Agent Platform")

class AgentRequest(BaseModel):
    question: str
    max_iterations: int = 5

@app.post("/agent/run")
async def run_agent(request: AgentRequest):
    client = OpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url=os.getenv("DEEPSEEK_BASE_URL"),
    )
    executor = AgentExecutor(llm_client=client, model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"), max_iterations=request.max_iterations)
    result = executor.run(request.question)
    return result

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    """返回任务详情以及按 iteration 排序的执行步骤。"""
    conn = sqlite3.connect("agent.db")
    conn.row_factory = sqlite3.Row
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
