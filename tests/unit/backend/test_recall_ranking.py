"""统一排序数学的单元测试（纯函数，无 DB / 无 IO）。

与 tests/integration/pg/test_pg_bm25_recall.py 互补：那里验证真实 PG + pg_search
的端到端行为，这里锁定 RRF / Recency / 归一化 / 加权公式的边界与缺失值处理。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aiive.memory.recall_ranking import (
    DEFAULT_HALF_LIFE_DAYS,
    EPISODIC_RECENCY_FACTOR_DEFAULT,
    RECENCY_HALF_LIFE_DAYS,
    fused_score,
    is_time_sensitive_query,
    normalize_scores,
    recency_decay,
    recency_signal,
    rrf_normalized,
    rrf_raw,
    trust_weight,
)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def test_rrf_single_route_rank_one_reaches_one():
    raw = rrf_raw([(1, 1.0)], k=60)
    assert rrf_normalized(raw, k=60, total_weight=1.0) == pytest.approx(1.0)


def test_rrf_two_routes_both_rank_one_reaches_one():
    raw = rrf_raw([(1, 1.0), (1, 1.0)], k=60)
    assert rrf_normalized(raw, k=60, total_weight=2.0) == pytest.approx(1.0)


def test_rrf_rank_decays_monotonically():
    first = rrf_normalized(rrf_raw([(1, 1.0)], k=60), k=60, total_weight=1.0)
    second = rrf_normalized(rrf_raw([(2, 1.0)], k=60), k=60, total_weight=1.0)
    tenth = rrf_normalized(rrf_raw([(10, 1.0)], k=60), k=60, total_weight=1.0)
    assert first > second > tenth > 0


def test_rrf_ignores_invalid_rank_and_zero_weight():
    assert rrf_raw([(0, 1.0)], k=60) == 0.0
    assert rrf_normalized(0.0, k=60, total_weight=0.0) == 0.0


def test_normalize_scores_uses_batch_max():
    assert normalize_scores([4.0, 2.0, 1.0]) == [1.0, 0.5, 0.25]


def test_normalize_scores_handles_empty_and_non_positive():
    assert normalize_scores([]) == []
    assert normalize_scores([0.0, -1.0]) == [0.0, 0.0]


def test_recency_decay_half_life():
    ref = NOW - timedelta(days=30)
    assert recency_decay(ref, half_life_days=30, now=NOW) == pytest.approx(0.5)
    assert recency_decay(None, half_life_days=30, now=NOW) == 0.0
    # 未来时间按 age=0 处理，不会产生 >1 的分
    assert recency_decay(NOW + timedelta(days=1), half_life_days=30, now=NOW) == 1.0


def test_recency_signal_is_type_aware():
    """同一时间点：情节记忆衰减快，长期偏好衰减慢。"""
    ref = NOW - timedelta(days=30)
    episodic = recency_signal("episodic", ref, "最近发生了什么", now=NOW)
    profile = recency_signal("user_profile", ref, "最近发生了什么", now=NOW)
    assert episodic < profile
    assert RECENCY_HALF_LIFE_DAYS["episodic"] < DEFAULT_HALF_LIFE_DAYS < RECENCY_HALF_LIFE_DAYS["user_profile"]


def test_recency_signal_episodic_needs_time_intent():
    """非时间敏感查询下，新近情节记忆的 recency 被折减，不能无条件压过长期偏好。"""
    fresh = NOW - timedelta(hours=1)
    ordinary = recency_signal("episodic", fresh, "手冲咖啡", now=NOW)
    time_sensitive = recency_signal("episodic", fresh, "最近喝的手冲咖啡", now=NOW)
    # 时间敏感查询全额计入（1 小时衰减后接近 1.0），普通查询再乘 0.35 折减系数
    assert time_sensitive == pytest.approx(recency_decay(fresh, half_life_days=3.0, now=NOW))
    assert ordinary == pytest.approx(time_sensitive * EPISODIC_RECENCY_FACTOR_DEFAULT, rel=1e-5)
    assert ordinary < time_sensitive


def test_is_time_sensitive_query_detects_chinese_and_english():
    assert is_time_sensitive_query("上次说的那个项目")
    assert is_time_sensitive_query("What did we discuss yesterday")
    assert not is_time_sensitive_query("用户偏好什么编辑器")


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("trusted", 1.0),
        ("semi_trusted", 0.7),
        ("untrusted", 0.3),
        ("untrusted_derived", 0.2),
        ("unknown_level", 0.5),
        ("", 0.5),
    ],
)
def test_trust_weight_missing_value_fallback(level, expected):
    assert trust_weight(level) == expected


def test_fused_score_matches_configured_weights():
    weights = {
        "rrf": 0.40, "relevance": 0.20, "recency": 0.20,
        "importance": 0.10, "trust": 0.05, "scope": 0.05,
    }
    score = fused_score(
        rrf_norm=1.0, relevance=1.0, recency=1.0, importance=1.0,
        trust="trusted", scope=1.0, weights=weights,
    )
    assert score == pytest.approx(1.0)

    half = fused_score(
        rrf_norm=0.0, relevance=0.0, recency=0.0, importance=0.0,
        trust="untrusted_derived", scope=0.0, weights=weights,
    )
    assert half == pytest.approx(0.05 * 0.2)


def test_fused_score_is_deterministic():
    kwargs = {
        "rrf_norm": 0.42, "relevance": 0.61, "recency": 0.33,
        "importance": 0.8, "trust": "semi_trusted", "scope": 0.75,
    }
    weights = {"rrf": 0.4, "relevance": 0.2, "recency": 0.2,
               "importance": 0.1, "trust": 0.05, "scope": 0.05}
    assert fused_score(**kwargs, weights=weights) == fused_score(**kwargs, weights=weights)
