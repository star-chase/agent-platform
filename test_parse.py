from app.agent import AgentExecutor
ex = AgentExecutor(None)
text = "Thought: 我需要搜索\nAction: search\nAction Input: Python"
print(ex._parse_llm_response(text))    # 期望得到 type=action, action=search, action_input=Python
