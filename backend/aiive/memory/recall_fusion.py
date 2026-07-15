"""Fusion, dedup, validity filtering, and token packing for recall candidates.

Combines candidates produced by multiple routes using Reciprocal Rank Fusion
(RRF) plus global signals (relevance, scope, recency, importance, trust).

Candidates are expected to be already lifecycle/validity-filtered (active +
valid) by the routes — except timeline/history queries which bypass the
ordinary pack. This module performs the ordinary-pack fusion only.
"""
from __future__ import annotations

from aiive.memory.recall_config import RecallConfig
from aiive.memory.recall_models import (
    MemoryRecallItem,
    MemoryRecallPack,
    MemoryRecallRequest,
    RecallCandidateTrace,
)

_TRUST_WEIGHT: dict[str, float] = {
    "trusted": 1.0,
    "semi_trusted": 0.7,
    "untrusted": 0.3,
    "untrusted_derived": 0.2,
}


def _trust_weight(level: str) -> float:
    return _TRUST_WEIGHT.get(level, 0.5)


def fuse_and_pack(
    candidates: list[MemoryRecallItem],
    request: MemoryRecallRequest,
    config: RecallConfig,
) -> tuple[MemoryRecallPack, list[RecallCandidateTrace]]:
    """Fuse candidates, dedup, filter by relevance, pack by token budget.

    Returns (MemoryRecallPack, candidate_traces).
    """
    # --- Dedup by memory_id: keep max relevance, union routes ---
    by_id: dict[str, MemoryRecallItem] = {}
    for c in candidates:
        existing = by_id.get(c.memory_id)
        if existing is None:
            by_id[c.memory_id] = c
        else:
            if c.relevance_score > existing.relevance_score:
                existing.relevance_score = c.relevance_score
            if c.route not in existing.route:
                existing.route = f"{existing.route}|{c.route}"

    items = list(by_id.values())

    # --- Per-route ranks for RRF ---
    route_ids: dict[str, list[str]] = {}
    for c in candidates:
        route_ids.setdefault(c.route, []).append(c.memory_id)

    k = config.rrf_k
    weights = config.route_weights
    traces: list[RecallCandidateTrace] = []

    for item in items:
        rrf = 0.0
        for _route, ids in route_ids.items():
            if item.memory_id in ids:
                rank = ids.index(item.memory_id) + 1
                rrf += 1.0 / (k + rank)
        fused = (
            weights.get("rrf", 0.40) * rrf
            + weights.get("relevance", 0.30) * item.relevance_score
            + weights.get("scope", 0.15) * item.scope_score
            + weights.get("recency", 0.05) * item.recency_score
            + weights.get("importance", 0.05) * item.importance_score
            + weights.get("trust", 0.05) * _trust_weight(item.trust_level)
        )
        item.fused_score = round(fused, 6)

    items.sort(key=lambda x: x.fused_score, reverse=True)

    selected: list[MemoryRecallItem] = []
    excluded = 0
    token_total = 0

    for item in items:
        # Exact-key route always passes the relevance threshold (precise match).
        if item.route.split("|")[0] == "exact":
            below = False
        else:
            below = item.relevance_score < config.recall_relevance_threshold

        if below:
            traces.append(RecallCandidateTrace(
                memory_id=item.memory_id, route=item.route,
                raw_score=item.relevance_score, fused_score=item.fused_score,
                selected=False, exclusion_reason="below_relevance_threshold",
                token_cost=item.token_cost,
            ))
            excluded += 1
            continue
        if token_total + item.token_cost > request.token_budget:
            traces.append(RecallCandidateTrace(
                memory_id=item.memory_id, route=item.route,
                raw_score=item.relevance_score, fused_score=item.fused_score,
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
