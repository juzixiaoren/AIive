"""统一排序：RRF 融合、去重、阈值过滤与 token 打包。

所有候选先由各自路由完成 lifecycle / validity / scope / Forget 过滤，本模块只做
排序数学，不再访问数据库。

排序公式（权重见 RecallConfig.signal_weights，和为 1.0）::

    fused = w_rrf      * rrf_norm
          + w_relevance* relevance      # 各路归一化到 0..1，不混量纲
          + w_recency  * recency        # 类型半衰期 × 查询意图
          + w_importance*importance
          + w_trust    * trust_weight
          + w_scope    * scope_score

    rrf_norm = min(1, rrf_raw * (k + 1) / total_route_weight)
    rrf_raw  = sum(route_weight / (k + rank_route))

不同路由的原始分（cosine、BM25、词重叠）不直接相加：BM25 与 cosine 只通过
各自归一化后的 relevance 与 RRF 排名参与排序。
"""
from __future__ import annotations

from aiive.memory.recall_config import RecallConfig
from aiive.memory.recall_models import (
    MemoryRecallItem,
    MemoryRecallPack,
    MemoryRecallRequest,
    RecallCandidateTrace,
)
from aiive.memory.recall_ranking import fused_score, rrf_normalized, rrf_raw


def fuse_and_pack(
    candidates: list[MemoryRecallItem],
    request: MemoryRecallRequest,
    config: RecallConfig,
) -> tuple[MemoryRecallPack, list[RecallCandidateTrace]]:
    """Fuse candidates, dedup, filter by relevance, pack by token budget.

    Returns (MemoryRecallPack, candidate_traces).
    """
    # --- Per-route ranks for RRF ---
    # 必须先于 dedup 分桶：dedup 会把候选对象的 route 就地改写为 "a|b" 联合串，
    # 若在 dedup 之后按 route 分桶，会产生 "bm25|vector" 之类的伪桶，
    # 使 RRF 排名漂移（同一记忆从原路由桶中消失）。
    route_ids: dict[str, list[str]] = {}
    for c in candidates:
        route_ids.setdefault(c.route, []).append(c.memory_id)

    # --- Dedup by memory_id: keep max relevance, union routes ---
    by_id: dict[str, MemoryRecallItem] = {}
    for c in candidates:
        existing = by_id.get(c.memory_id)
        if existing is None:
            by_id[c.memory_id] = c
            continue
        existing.relevance_score = max(existing.relevance_score, c.relevance_score)
        existing.raw_route_score = max(existing.raw_route_score, c.raw_route_score)
        if c.route not in existing.route:
            existing.route = f"{existing.route}|{c.route}"

    items = list(by_id.values())

    k = config.rrf_k
    weights = config.signal_weights
    multipliers = config.route_multipliers
    total_route_weight = sum(multipliers.get(route, 1.0) for route in route_ids) or 1.0

    traces: list[RecallCandidateTrace] = []

    for item in items:
        ranks: list[tuple[int, float]] = []
        for route, ids in route_ids.items():
            if item.memory_id in ids:
                rank = ids.index(item.memory_id) + 1
                ranks.append((rank, multipliers.get(route, 1.0)))
        rrf_norm = rrf_normalized(
            rrf_raw(ranks, k), k=k, total_weight=total_route_weight,
        )
        item.fused_score = fused_score(
            rrf_norm=rrf_norm,
            relevance=item.relevance_score,
            recency=item.recency_score,
            importance=item.importance_score,
            trust=item.trust_level,
            scope=item.scope_score,
            weights=weights,
        )

    # 确定性排序：同分按 memory_id 升序，避免依赖插入顺序。
    items.sort(key=lambda x: (-x.fused_score, x.memory_id))

    selected: list[MemoryRecallItem] = []
    excluded = 0
    token_total = 0

    for item in items:
        # Exact-key route always passes the relevance threshold (precise match).
        # 确定性候选不因 RRF / 相关性不足被丢弃，只受 token 预算约束。
        if "exact" in item.route.split("|"):
            below = False
        else:
            below = item.relevance_score < config.recall_relevance_threshold

        if below:
            traces.append(RecallCandidateTrace(
                memory_id=item.memory_id, route=item.route,
                raw_score=item.relevance_score, fused_score=item.fused_score,
                route_raw_score=item.raw_route_score,
                selected=False, exclusion_reason="below_relevance_threshold",
                token_cost=item.token_cost,
            ))
            excluded += 1
            continue
        if token_total + item.token_cost > request.token_budget:
            traces.append(RecallCandidateTrace(
                memory_id=item.memory_id, route=item.route,
                raw_score=item.relevance_score, fused_score=item.fused_score,
                route_raw_score=item.raw_route_score,
                selected=False, exclusion_reason="token_budget_exceeded",
                token_cost=item.token_cost,
            ))
            excluded += 1
            continue
        selected.append(item)
        token_total += item.token_cost
        traces.append(RecallCandidateTrace(
            memory_id=item.memory_id, route=item.route,
            raw_score=item.relevance_score, fused_score=item.fused_score,
            route_raw_score=item.raw_route_score,
            selected=True, token_cost=item.token_cost,
        ))
        if len(selected) >= request.top_k:
            break

    pack = MemoryRecallPack(
        request_id=request.request_id,
        items=selected,
        token_count=token_total,
        excluded_count=excluded,
    )
    return pack, traces
