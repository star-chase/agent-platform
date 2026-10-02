from typing import Callable, Dict, Any

def calculator(expression: str) -> str:
    """计算数学表达式

    Args:
        expression: 数学表达式，如 "2 + 3 * 4"

    Returns:
        计算结果或错误信息
    """
    try:
        # 注意：生产环境不要用 eval，这里仅用于演示
        result = eval(expression)
        return f"计算结果: {result}"
    except Exception as e:
        return f"计算错误: {str(e)}"


def search(query: str) -> str:
    """模拟搜索引擎查询

    Args:
        query: 搜索关键词

    Returns:
        搜索结果
    """
    # Mock 数据库
    mock_db = {
        "北京": "北京是中国的首都，人口约2100万",
        "上海": "上海是中国最大的城市，人口约2500万",
        "Python": "Python 是一种高级编程语言，由 Guido van Rossum 于1991年创建",
        "Agent": "Agent 是一种能够感知环境并采取行动以实现目标的实体",
    }
    hits = []
    for key in mock_db:
        if key in query:
            hits.append(mock_db[key])
    if hits:
        return "\n".join(hits)
    else:
        return mock_db.get(query, f"未找到关于 '{query}' 的信息")
    


# 阶段 6-D：控制面工具（不是业务工具）。
# 它没有 Python 实现 —— 执行器会拦截这次调用，把任务停下来等人类批准，
# 而不是像 search/calculator 那样"执行并返回结果"。
REQUEST_APPROVAL_TOOL = {
    "type": "function",
    "function": {
        "name": "request_human_approval",
        "description": (
            "在执行不可逆或对外的动作（对外发布、发邮件/消息、花钱、删数据、写外部系统）之前，"
            "先请求人类批准。调用后本任务会暂停，等人类批准或拒绝。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "为什么这一步需要人类批准"},
                "proposed_action": {"type": "string", "description": "你打算具体做什么"},
            },
            "required": ["reason", "proposed_action"],
        },
    },
}


# 工具注册表：工具名 -> 工具函数
TOOLS: Dict[str, Callable] = {
    "calculator": calculator,
    "search": search,
}


def get_tool_descriptions() -> str:
    """生成工具描述文本，用于 LLM 提示词

    Returns:
        格式化的工具描述
    """
    descriptions = []
    for name, func in TOOLS.items():
        doc = func.__doc__.strip() if func.__doc__ else "无描述"
        # 提取第一行作为简短描述
        short_desc = doc.split('\n')[0]
        descriptions.append(f"- {name}: {short_desc}")

    return "\n".join(descriptions)

tools = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "计算数学表达式，如 2+3*4",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "要计算的数学表达式，如 '2+3*4'"
                    }
                },
                "required": ["expression"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "搜索关键词",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词,比如'北京'"
                    }
                },
                "required": ["query"]
            }
        }
    }
]
