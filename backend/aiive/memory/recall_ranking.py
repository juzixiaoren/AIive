"""统一排序的纯数学部分：RRF、Recency、归一化与缺失值处理。

本模块不访问数据库、不做 IO，全部是确定性纯函数，便于单测与文档对齐
（docs/architecture/memory_retrieval_bm25.md 中的公式与本文件一一对应）。

排序信号共六项：

1. rrf        —— Reciprocal Rank Fusion，只用各路排名，不混量纲。
2. relevance  —— 各路归一化到 0..1 的相关性（dense=cosine，sparse=BM25/批内最高分，
                 lexical=词重叠比例）。
3. recency    —— 记忆类型半衰期 × 查询意图系数的时间衰减。
4. importance —— MemoryRecord.importance（0..1）。
5. trust      —— trust_level 映射（trusted/semi_trusted/untrusted/...）。
6. scope      —— Scope 链位置（thread 最高，global 最低）。
"""
from __future__ import annotations

from datetime import UTC, datetime

# ---------------------------------------------------------------------------
# Recency：记忆类型半衰期（天）
# ---------------------------------------------------------------------------

# 情节记忆衰减快、稳定偏好/事实衰减慢。半衰期越长，越不容易被「刚发生」
# 的事件挤掉；episodic 的短半衰期只在时间敏感查询里全额生效。
RECENCY_HALF_LIFE_DAYS: dict[str, float] = {
    "episodic": 3.0,
    "event": 3.0,
    "task": 7.0,
    "user_profile": 365.0,
    "agent_self": 365.0,
    "preference": 365.0,
    "knowledge": 180.0,
}
DEFAULT_HALF_LIFE_DAYS: float = 90.0

# 非时间敏感查询下情节记忆的 recency 折减系数。保证「最近发生」本身不足以
# 压过长期偏好与重要事实。
EPISODIC_RECENCY_FACTOR_DEFAULT: float = 0.35
# 时间敏感查询下情节记忆全额计入 recency。
EPISODIC_RECENCY_FACTOR_TIME_SENSITIVE: float = 1.0

# 显式时间意图标记（中英混合，大小写不敏感）。
_TIME_SENSITIVE_MARKERS: tuple[str, ...] = (
    "最近", "近期", "上次", "上回", "刚才", "刚刚", "今天", "昨天", "前天",
    "这周", "本周", "上周", "这个月", "上个月", "今年", "去年", "之前", "后来",
    "recent", "recently", "latest", "last time", "yesterday", "today",
    "this week", "last week", "this month", "last month", "earlier",
)

_EPISODIC_TYPES: frozenset[str] = frozenset({"episodic", "event"})

_TRUST_WEIGHT: dict[str, float] = {
    "trusted": 1.0,
    "semi_trusted": 0.7,
    "untrusted": 0.3,
    "untrusted_derived": 0.2,
}
DEFAULT_TRUST_WEIGHT: float = 0.5


def is_time_sensitive_query(query: str) -> bool:
    """查询是否带有显式时间意图（决定情节记忆的 recency 是否全额计入）。"""
    q = (query or "").lower()
    return any(marker in q for marker in _TIME_SENSITIVE_MARKERS)


def trust_weight(level: str) -> float:
    """trust_level → 0..1 权重；未知取值按 0.5（缺失值兜底）。"""
    return _TRUST_WEIGHT.get(level, DEFAULT_TRUST_WEIGHT)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def recency_decay(
    reference: datetime | None,
    *,
    half_life_days: float,
    now: datetime | None = None,
) -> float:
    """指数衰减 0.5 ** (age_days / half_life_days)，输出 0..1。

    缺失时间戳（None）返回 0.0；未来时间按 age=0 处理（不产生 >1 的分）。
    """
    ref = _as_utc(reference)
    if ref is None:
        return 0.0
    current = _as_utc(now) or datetime.now(UTC)
    age_days = max(0.0, (current - ref).total_seconds() / 86400.0)
    if half_life_days <= 0:
        return 0.0
    return round(0.5 ** (age_days / half_life_days), 6)


def recency_signal(
    memory_type: str,
    observed_at: datetime | None,
    query: str,
    *,
    now: datetime | None = None,
) -> float:
    """类型感知 + 意图感知的 recency 信号（0..1）。

    公式::

        recency = decay(type_half_life, age) * intent_factor(type, query)

    其中 intent_factor 只作用于情节记忆：时间敏感查询为 1.0，否则为 0.35。
    长期偏好/知识/身份事实恒为 1.0，因此「最近」不会自动压过它们。
    """
    mtype = (memory_type or "").strip().lower()
    half_life = RECENCY_HALF_LIFE_DAYS.get(mtype, DEFAULT_HALF_LIFE_DAYS)
    decay = recency_decay(observed_at, half_life_days=half_life, now=now)
    if mtype in _EPISODIC_TYPES:
        factor = (
            EPISODIC_RECENCY_FACTOR_TIME_SENSITIVE
            if is_time_sensitive_query(query)
            else EPISODIC_RECENCY_FACTOR_DEFAULT
        )
        decay = decay * factor
    return round(min(1.0, max(0.0, decay)), 6)


def normalize_scores(scores: list[float]) -> list[float]:
    """按批内最高分归一化到 0..1（BM25 原始分不是 0..1 量纲）。

    最高分 <= 0 或列表为空时返回全 0，避免除零；结果保持入参顺序。
    """
    if not scores:
        return []
    top = max(scores)
    if top <= 0:
        return [0.0 for _ in scores]
    return [round(max(0.0, s) / top, 6) for s in scores]


def rrf_raw(ranks: list[tuple[int, float]], k: int) -> float:
    """Reciprocal Rank Fusion：sum(w_route / (k + rank))。

    ranks 为 [(rank, route_weight), ...]，rank 从 1 开始。
    """
    total = 0.0
    for rank, weight in ranks:
        if rank <= 0:
            continue
        total += weight / (k + rank)
    return total


def rrf_normalized(raw: float, *, k: int, total_weight: float) -> float:
    """把 RRF 原始分线性映射到 0..1。

    当某条记忆在全部参与路由中都排第 1 时取到 1.0：

        rrf_norm = raw * (k + 1) / max(total_weight, 1e-9)
    """
    if total_weight <= 0:
        return 0.0
    return round(min(1.0, raw * (k + 1) / total_weight), 6)


def fused_score(
    *,
    rrf_norm: float,
    relevance: float,
    recency: float,
    importance: float,
    trust: str,
    scope: float,
    weights: dict[str, float],
) -> float:
    """统一排序最终分：各信号按配置权重加权求和（权重和归一化为 1）。

    缺失值处理：importance/scope/recency 由调用方给出 0..1 默认值（0.5/0.0/0.0），
    trust 未知取值走 0.5，relevance 缺失为 0.0。结果四舍五入到 6 位，
    保证同输入同输出（确定性排序）。
    """
    w = weights
    value = (
        w.get("rrf", 0.0) * rrf_norm
        + w.get("relevance", 0.0) * relevance
        + w.get("recency", 0.0) * recency
        + w.get("importance", 0.0) * importance
        + w.get("trust", 0.0) * trust_weight(trust)
        + w.get("scope", 0.0) * scope
    )
    return round(value, 6)
