import sqlite3
from pathlib import Path

# 创建数据库文件
db_path = Path("/home/asus/projects/agent-platform/agent.db")
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# 创建 tasks 表
cursor.execute("""
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    answer TEXT,
    max_iterations INTEGER DEFAULT 5,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)
""")

# 创建 steps 表
cursor.execute("""
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
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    UNIQUE (task_id, iteration)
)
""")

conn.commit()
conn.close()

print(f"✅ 数据库已创建: {db_path}")
print("✅ tasks 表已创建")
print("✅ steps 表已创建")
