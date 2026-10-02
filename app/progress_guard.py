"""无进展检测：防止 Agent 反复调用工具、拿到同样的结果却不收尾。

2026-09-17 真实联调第一次复现了这个阶段 1 就留下的问题（任务 1-F / 思考题 7）：
调研类节点连续 5 轮调用 search，拿到的都是同一句"北京是中国的首都，人口约 2100 万"，
最后耗尽 max_iterations 被判 FAILED。**Agent 会烧钱，但不会自己停下来。**

判定思路：一轮里拿到的工具结果**全部**都是以前见过的 → 这一轮没有任何新信息。
连续 N 轮没有新信息 → 判定为"在做无用功"，应该强制收尾。

为什么按"新信息"而不是"参数完全相同"来判：
真实 LLM 每次会换关键词（"北京人口"→"北京市常住人口"→"tjj.beijing.gov.cn 人口"），
参数并不相同，但对 mock 工具来说返回的却是同一句——参数比对抓不到，结果比对能抓到。
"""

NO_PROGRESS_HINT = (
    "注意：你刚才的工具调用没有获得任何新信息（结果与之前完全相同）。"
    "请不要再重复调用工具，直接基于已有信息给出最终答案。"
)


class NoProgressDetector:
    """检测 Agent 是否在原地打转。"""

    def __init__(self, max_no_progress_rounds: int = 2):
        if max_no_progress_rounds < 1:
            raise ValueError("max_no_progress_rounds 必须 >= 1")
        self.max_no_progress_rounds = max_no_progress_rounds
        self._seen_observations: set[str] = set()
        self.consecutive_no_progress = 0

    def observe(self, observations: list[str]) -> bool:
        """记录一轮的工具结果，返回是否已达到"该强制收尾"的阈值。"""
        normalized = {str(observation) for observation in observations}
        has_new_information = bool(normalized - self._seen_observations)
        self._seen_observations |= normalized
        if has_new_information:
            self.consecutive_no_progress = 0
            return False
        self.consecutive_no_progress += 1
        return self.consecutive_no_progress >= self.max_no_progress_rounds

    def reset(self) -> None:
        """续跑（resume）时清空历史。"""
        self._seen_observations = set()
        self.consecutive_no_progress = 0
