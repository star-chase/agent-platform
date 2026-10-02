"""成本核算（阶段 7-A）。

三大支柱里的 **Metrics** 在本项目的第一步：把"花了多少钱"算清楚。

为什么价格要单独抽出来配置化：
- 单价会变（厂商调价、出新模型），写死在业务代码里改一次要动一片；
- 不同模型单价不同，要能按模型查；
- 单元测试里可以注入假价格，算出来的数字可预期。
"""

from typing import Any

from app.db import connect

# ⚠️ 示例价格（美元 / 每 100 万 token）。
# 真实价格必须去厂商价目页核对后再填 —— 这里的数值只用于演示计算逻辑，
# 不要拿去算真实账单。改这里不需要动任何业务代码。
MODEL_PRICES: dict[str, dict[str, float]] = {
    "deepseek-v4-flash": {"input": 0.27, "output": 1.10},
}
DEFAULT_PRICE = {"input": 0.27, "output": 1.10}

TOKENS_PER_UNIT = 1_000_000


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """按模型单价估算一次（或一批）调用的成本，单位美元。

    注意 input 和 output 的单价是分开的，而且通常差好几倍 ——
    这也是"每轮都要重发历史"（input 膨胀）为什么那么贵的原因。
    """
    price = MODEL_PRICES.get(model, DEFAULT_PRICE)
    cost = (input_tokens / TOKENS_PER_UNIT) * price["input"]
    cost += (output_tokens / TOKENS_PER_UNIT) * price["output"]
    return round(cost, 6)


def check_budget(spent_usd: float, budget_usd: float | None) -> dict[str, Any]:
    """预算闸门的**判断**这一半（纯函数，不碰数据库）。

    返回 {"budget_usd": ..., "spent_usd": ..., "remaining_usd": ..., "exceeded": bool}

    规则（边界值也要想清楚，测试里有）：
    - budget_usd 是 None → 不设预算，永远 exceeded=False，remaining_usd 也是 None
      （None 是"没有上限"，和数字 0 是两件完全不同的事 ——
       单 Agent 的 /agent/run 没有 workflow、没有预算，不能因为查不到就把它拦下来）
    - spent >= budget → exceeded=True。**用满即停**：钱已经花完了，下一笔调用必然越界，
      所以"正好花完"必须算超支，不能在边界上留一条缝。
    - remaining_usd = 预算 - 已花，可以是负数（负多少 = 超了多少）。
      不要 clamp 成 0 —— 这个数字是给人看"超支多严重"的。
    """
    if budget_usd is None:
        return {
            "budget_usd": None,
            "spent_usd": spent_usd,
            "remaining_usd": None,
            "exceeded": False
        }
    exceeded = spent_usd >= budget_usd
    remaining_usd = budget_usd - spent_usd
    return {
        "budget_usd": budget_usd,
        "spent_usd": spent_usd,
        "remaining_usd": remaining_usd,
        "exceeded": exceeded
    }


def task_budget_status(db_path: str | None, task_id: str, model: str) -> dict[str, Any]:
    """给执行器用的版本 —— 从 task 反查出它所属 workflow 的预算，再判闸门。

    执行器手里只有 task_id，而预算挂在 workflow 上，中间隔了一跳：
        tasks.workflow_id  →  workflows.budget_usd
    （为什么不把 budget_usd 一路当参数传下来：那样每个 new 执行器的地方都要记得传，
      漏传一处预算就静默失效。**数据库里那份才是唯一事实来源**。）

    返回 check_budget(...) 的结果，另外带上 workflow_id（日志/事件里要说清是哪个
    workflow 超的）。任务不存在、或不属于任何 workflow（单 Agent 运行）→ 当作"无预算"。

    这条查询会在**每一次 LLM 调用之前**跑，所以只查必需的那两列，别 SELECT *。

    三个曾经踩过的坑（都在这一个函数里）：
    1. 忘了算 **spent** —— 把 0 传进 check_budget，闸门就只会拦 budget<=0 的情况，
       等于整个预算防线静默失效。
    2. `dict.update()` **返回 None**，不能 `return d.update({...})`：那样函数返回 None，
       调用方一句 status["exceeded"] 就是 TypeError。要 `d.update(...)` 之后再 return d。
    3. 没设预算时不要去查成本 —— 省一次全表 SUM，而且那个数字本来也没人会看。
    """
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT t.workflow_id, w.budget_usd FROM tasks t "
            "LEFT JOIN workflows w ON t.workflow_id = w.id "
            "WHERE t.id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()

    # 任务不存在、不属于任何 workflow、或 workflow 查不到预算 → 放行（无预算可言）
    if row is None or row[0] is None or row[1] is None:
        return {"workflow_id": row[0] if row else None, **check_budget(0.0, None)}

    workflow_id, budget_usd = row[0], row[1]
    # 花了多少必须现查：预算要按**最新**的累计花费判，缓存一份在内存里就会漏判
    spent_usd = workflow_cost(db_path, workflow_id, model)["cost_usd"]
    return {"workflow_id": workflow_id, **check_budget(spent_usd, budget_usd)}


def task_cost(db_path: str | None, task_id: str, model: str) -> dict[str, Any]:
    """汇总一个任务的 token 与成本。

    返回：{"input_tokens": ..., "output_tokens": ..., "cost_usd": ...}

    注意 COALESCE：老数据的 token 列是 NULL，不加它 SUM 会返回 NULL，
    前端拿到的就不是数字而是 null。
    """
    conn = connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT 
            COALESCE(SUM(input_tokens), 0) AS total_input_tokens,
            COALESCE(SUM(output_tokens), 0) AS total_output_tokens
        FROM steps
        WHERE task_id = ?
        """,
        (task_id,)
    )
    result = cursor.fetchone()
    total_input_tokens = result[0]
    total_output_tokens = result[1]
    cost = estimate_cost(model, total_input_tokens, total_output_tokens)

    return {
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "cost_usd": cost
    }


def workflow_cost(db_path: str | None, workflow_id: str, model: str) -> dict[str, Any]:
    """汇总一个 workflow 下**所有子任务**的 token 与成本。

    返回：{"input_tokens": ..., "output_tokens": ..., "cost_usd": ...}

    steps 表里没有 workflow_id，所以必须 JOIN tasks 才能按 workflow 汇总。
    （真实取舍：每个表都冗余一份 workflow_id 查询更快，但一致性要自己保证；
      JOIN 一致，但查询更重。）
    """
    conn = connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT 
            COALESCE(SUM(s.input_tokens), 0) AS total_input_tokens,
            COALESCE(SUM(s.output_tokens), 0) AS total_output_tokens
        FROM steps s
        JOIN tasks t ON s.task_id = t.id
        WHERE t.workflow_id = ?
        """,
        (workflow_id,)
    )
    result = cursor.fetchone()
    total_input_tokens = result[0]
    total_output_tokens = result[1]
    cost = estimate_cost(model, total_input_tokens, total_output_tokens)

    return {
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "cost_usd": cost
    }
