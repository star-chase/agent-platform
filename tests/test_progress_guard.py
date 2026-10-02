"""无进展检测的纯逻辑测试（不涉及 LLM / 数据库）。"""

import pytest

from app.progress_guard import NoProgressDetector


def test_new_information_counts_as_progress():
    detector = NoProgressDetector(max_no_progress_rounds=2)
    assert detector.observe(["北京约 2100 万"]) is False
    assert detector.observe(["上海约 2500 万"]) is False   # 新信息 → 重置
    assert detector.consecutive_no_progress == 0


def test_repeated_observations_reach_threshold():
    detector = NoProgressDetector(max_no_progress_rounds=2)
    assert detector.observe(["北京约 2100 万"]) is False      # 第一次见到，算进展
    assert detector.observe(["北京约 2100 万"]) is False      # 第 1 轮无新信息
    assert detector.observe(["北京约 2100 万"]) is True       # 第 2 轮无新信息 → 该收尾了


def test_partially_new_round_still_counts_as_progress():
    """只要有一点点新信息，就不算原地打转（避免误杀）。"""
    detector = NoProgressDetector(max_no_progress_rounds=2)
    detector.observe(["A"])
    detector.observe(["A"])                       # 无新信息（第 1 次）
    assert detector.observe(["A", "B"]) is False  # 出现了 B → 有进展，重置
    assert detector.consecutive_no_progress == 0


def test_reset_clears_history():
    detector = NoProgressDetector(max_no_progress_rounds=2)
    detector.observe(["A"])
    detector.observe(["A"])
    detector.reset()
    assert detector.observe(["A"]) is False
    assert detector.consecutive_no_progress == 0


def test_invalid_threshold_is_rejected():
    with pytest.raises(ValueError, match="max_no_progress_rounds"):
        NoProgressDetector(max_no_progress_rounds=0)
