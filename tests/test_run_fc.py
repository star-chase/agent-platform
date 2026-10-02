import os
from dotenv import load_dotenv
from openai import OpenAI
from app.agent_fc import AgentFCExecutor
import json

load_dotenv("/home/asus/projects/agent-platform/.env")
client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL"),
)
executor = AgentFCExecutor(llm_client=client, model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"), max_iterations=5)
result = executor.run("上海有多少人口？请先搜索，然后计算人口数除以10")
print(json.dumps(result, ensure_ascii=False, indent=2))
