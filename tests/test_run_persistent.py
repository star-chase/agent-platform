import os
from dotenv import load_dotenv
from openai import OpenAI
from app.agent_fc_persistent import AgentFCPersistentExecutor
import json

load_dotenv("/home/asus/projects/agent-platform/.env")
client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL"),
)

print("🚀 运行持久化版本的 Agent...")
executor = AgentFCPersistentExecutor(
    llm_client=client,
    model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
    max_iterations=5,
    db_path="/home/asus/projects/agent-platform/agent.db"
)

result = executor.run("上海有多少人口？请先搜索，然后计算人口数除以10")
print("\n" + "="*60)
print("📊 执行结果：")
print(json.dumps(result, ensure_ascii=False, indent=2))
print("="*60)

# 验证数据库
print("\n🔍 验证数据库记录...\n")
import sqlite3
conn = sqlite3.connect("/home/asus/projects/agent-platform/agent.db")
cursor = conn.cursor()

# 查询 tasks 表
cursor.execute("SELECT id, question, status, answer FROM tasks ORDER BY created_at DESC LIMIT 1")
task = cursor.fetchone()
print(f"✅ Tasks 表最新记录:")
print(f"   ID: {task[0]}")
print(f"   Question: {task[1]}")
print(f"   Status: {task[2]}")
print(f"   Answer: {task[3][:50]}..." if task[3] else "   Answer: None")

# 查询 steps 表
cursor.execute("SELECT iteration, type, tool_name, tool_args, observation FROM steps WHERE task_id = ? ORDER BY iteration", (task[0],))
steps = cursor.fetchall()
print(f"\n✅ Steps 表记录 (共 {len(steps)} 条):")
for step in steps:
    print(f"   [{step[0]}] {step[1]} - {step[2] or 'N/A'}")
    if step[3]:
        print(f"       Args: {step[3]}")
    if step[4]:
        print(f"       Result: {step[4][:60]}...")

conn.close()
