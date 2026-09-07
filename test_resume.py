import os
from dotenv import load_dotenv
from openai import OpenAI
from app.agent_fc_persistent import AgentFCPersistentExecutor
import json
import sqlite3

load_dotenv("/home/asus/projects/agent-platform/.env")
client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL"),
)

print("=" * 70)
print("🧪 测试场景：断点续跑")
print("=" * 70)

# 第一步：运行一个正常任务，获取 task_id
print("\n📝 步骤 1：先运行一个完整任务，拿到 task_id...")
executor = AgentFCPersistentExecutor(
    llm_client=client,
    model=os.getenv("DEEPSEEK_MODEL"),
    max_iterations=5,
    db_path="/home/asus/projects/agent-platform/agent.db"
)

result = executor.run("北京有多少人口？")
task_id = result["task_id"]
print(f"✅ 任务完成，task_id: {task_id}")
print(f"   答案: {result['answer'][:50]}...")
print(f"   执行了 {result['iterations']} 轮")

# 第二步：查看数据库记录
print(f"\n🔍 步骤 2：查看数据库中的记录...")
conn = sqlite3.connect("/home/asus/projects/agent-platform/agent.db")
cursor = conn.cursor()
cursor.execute("SELECT iteration, type, tool_name FROM steps WHERE task_id = ? ORDER BY iteration", (task_id,))
steps = cursor.fetchall()
print(f"   数据库中有 {len(steps)} 条 step 记录:")
for step in steps:
    print(f"     [{step[0]}] {step[1]} - {step[2] or 'N/A'}")
conn.close()

# 第三步：测试 resume（虽然任务已完成，但验证 resume 的读取逻辑）
print(f"\n🔄 步骤 3：尝试 resume 已完成的任务（应该提示无需续跑）...")
resume_result = executor.resume(task_id)
print(f"   结果: {resume_result}")

# 第四步：创建一个"假装失败"的任务来真正测试 resume
print(f"\n💥 步骤 4：手动模拟一个中途失败的任务...")
conn = sqlite3.connect("/home/asus/projects/agent-platform/agent.db")
cursor = conn.cursor()

# 创建一个 FAILED 状态的任务
import uuid
fake_task_id = str(uuid.uuid4())
cursor.execute("""
    INSERT INTO tasks (id, question, status, max_iterations)
    VALUES (?, '上海人口是多少？请先搜索再计算除以5', 'FAILED', 5)
""", (fake_task_id,))

# 插入第 1 步（search，已完成，但缺少原始 LLM message），验证旧数据被安全拒绝。
cursor.execute("""
    INSERT INTO steps (id, task_id, iteration, type, tool_name, tool_args, observation)
    VALUES (?, ?, 1, 'tool_call', 'search', '{"query": "上海人口"}', '上海是中国最大的城市，人口约2500万')
""", (str(uuid.uuid4()), fake_task_id))

conn.commit()
conn.close()

print(f"   ✅ 已创建假任务 {fake_task_id}，状态 FAILED，有 1 条完成的 step")

# 第五步：resume 这个旧数据任务
print(f"\n🚀 步骤 5：Resume 旧数据任务（应该明确提示无法安全续跑）...")
new_executor = AgentFCPersistentExecutor(
    llm_client=client,
    model=os.getenv("DEEPSEEK_MODEL"),
    max_iterations=5,
    db_path="/home/asus/projects/agent-platform/agent.db"
)

resume_result = new_executor.resume(fake_task_id)
assert resume_result.get("success") is False, "Resume 应该失败，但返回 success=True"
assert "缺少 llm_message_json" in resume_result.get("error", ""), "错误信息没有说明缺少 llm_message_json"
print("\n" + "=" * 70)
print("📊 Resume 结果:")
print(json.dumps(resume_result, ensure_ascii=False, indent=2))
print("=" * 70)

# 验证数据库
print(f"\n🔍 步骤 6：验证 resume 后数据库的变化...")
conn = sqlite3.connect("/home/asus/projects/agent-platform/agent.db")
cursor = conn.cursor()
cursor.execute("SELECT status FROM tasks WHERE id = ?", (fake_task_id,))
status = cursor.fetchone()[0]
print(f"   任务状态: {status}")

cursor.execute("SELECT iteration, type, tool_name FROM steps WHERE task_id = ? ORDER BY iteration", (fake_task_id,))
steps = cursor.fetchall()
assert len(steps) == 1, f"Resume 后 steps 应该只有 1 条，但实际有 {len(steps)} 条"
print(f"   Steps 记录 (共 {len(steps)} 条):")
for step in steps:
    print(f"     [{step[0]}] {step[1]} - {step[2] or 'N/A'}")
conn.close()

print("\n✅ 断点续跑测试完成！")
