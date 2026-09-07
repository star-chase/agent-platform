import sqlite3

# 连接数据库
conn = sqlite3.connect("/home/asus/projects/agent-platform/agent.db")
cursor = conn.cursor()

# 给 steps 表增加一列存 LLM 原始响应（JSON 字符串）
try:
    cursor.execute("ALTER TABLE steps ADD COLUMN llm_message_json TEXT")
    print("✅ 已添加列: llm_message_json")
except sqlite3.OperationalError as e:
    if "duplicate column name" in str(e):
        print("⚠️  列 llm_message_json 已存在，跳过")
    else:
        raise

conn.commit()

# 验证表结构
cursor.execute("PRAGMA table_info(steps)")
columns = cursor.fetchall()
print("\n📋 steps 表结构:")
for col in columns:
    print(f"   {col[1]} ({col[2]})")

conn.close()
print("\n✅ 表结构更新完成")
