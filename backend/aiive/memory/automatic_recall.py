"""Automatic Recall engine — query-aware, multi-route, runs every turn.

Routes:
  1. Exact / Structured  —— canonical_key / entity / scope-id match
  2. PostgreSQL FTS       —— query really participates (ILIKE word overlap)
  3. Vector               —— adapter stub (Qdrant not wired); returns []
  4. Temporal Graph       —— adapter stub (KG not wired); returns []
  5. Recent Episode       —— recent episodic records in scope (fallback context)

Routes never run a full `get_active()` and filter in Python; each route issues
scope-bounded SQL. Candidates are fused by `recall_fusion.fuse_and_pack`.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# CJK-aware tokenization — avoids the \W+ pitfall where Chinese text
# (all word-characters under Python 3 re.UNICODE) sticks together as a
# single token. Overlapping bigrams from CJK runs are appended to the
# standard \W+ token list so character-level matching works.
# ---------------------------------------------------------------------------

_CJK_RE = re.compile("[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]+")


def _tokenize_cjk_aware(text: str, min_len: int = 2) -> list[str]:
    """Split text into tokens: \\W+ for Latin/punctuation, overlapping bigrams for CJK.

    Example: "我喜欢咖啡 preference" → ["我喜欢咖啡", "我喜", "喜欢", "欢咖", "咖啡", "preference"]
    """
    tokens: list[str] = []
    for raw_token in re.split(r"\W+", text):
        raw = raw_token.strip()
        if len(raw) < min_len:
            continue
        tokens.append(raw)
        # For CJK runs, add overlapping n-grams so partial matches work
        for m in _CJK_RE.finditer(raw):
            cjk = m.group()
            for i in range(len(cjk) - min_len + 1):
                tokens.append(cjk[i:i + min_len])
    return tokens

from sqlalchemy import or_
from sqlalchemy.orm import Session

from aiive.db.models import MemoryRecord
from aiive.memory.memory_store import MemoryStore
from aiive.memory.memory_types import LifecycleState, ValidityState
from aiive.memory.recall_config import RecallConfig
from aiive.memory.recall_fusion import fuse_and_pack
from aiive.memory.recall_models import (
    MemoryRecallItem,
    MemoryRecallPack,
    MemoryRecallRequest,
    RecallCandidateTrace,
)

_KEYISH = re.compile(r"^[A-Za-z0-9_.]+$")


def _recency_score(r: MemoryRecord) -> float:
    """Recency in 0..1 over a 30-day window."""
    ref = r.observed_at or r.updated_at or r.created_at
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - ref).total_seconds() / 86400.0
    return round(max(0.0, 1.0 - age_days / 30.0), 3)


def _scope_score(actual_scope_type: str, actual_scope_id: str | None, chain: list[tuple[str, str | None]]) -> float:
    """Higher score for tighter scope (thread > ... > global).

    Matches (scope_type, scope_id) precisely; falls back to scope_type-only
    match when exact pair is not found (e.g. for records inserted before chain
    scopes were fully specified).
    """
    # Exact (scope_type, scope_id) match
    try:
        idx = next(
            i for i, s in enumerate(chain)
            if s[0] == actual_scope_type and s[1] == actual_scope_id
        )
    except StopIteration:
        # Fall back to scope_type-only match
        try:
            idx = next(i for i, s in enumerate(chain) if s[0] == actual_scope_type)
        except StopIteration:
            return 0.0
    return round(1.0 - idx / max(1, len(chain)), 3)


class AutomaticRecallEngine:
    """Runs Automatic Recall once per user turn (or per Agent memory tool call)."""

    def __init__(self, db: Session, config: RecallConfig | None = None) -> None:
        self._db: Session = db
        self._store: MemoryStore = MemoryStore(db)
        self._config: RecallConfig = config or RecallConfig()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def recall(
        self, request: MemoryRecallRequest
    ) -> tuple[MemoryRecallPack, list[RecallCandidateTrace]]:
        """Execute all enabled routes and fuse into a pack."""
        candidates: list[MemoryRecallItem] = []
        candidates += self._route_exact(request)
        candidates += self._route_fts(request)
        candidates += self._route_vector(request)
        candidates += self._route_temporal_graph(request)
        candidates += self._route_recent_episode(request)
        return fuse_and_pack(candidates, request, self._config)

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    def _route_exact(self, request: MemoryRecallRequest) -> list[MemoryRecallItem]:
        """Match canonical_key / scope-id precisely. High precision → passes threshold."""
        q = request.query.strip()
        if not q:
            return []
        chain = request.scope_context.chain()
        if not _KEYISH.match(q):
            return []
        out: list[MemoryRecallItem] = []
        for scope_type, _scope_id in chain:
            recs = (
                self._db.query(MemoryRecord)
                .filter(
                    MemoryRecord.lifecycle_state == LifecycleState.ACTIVE.value,
                    MemoryRecord.validity_state == ValidityState.VALID.value,
                    MemoryRecord.scope_type == scope_type,
                    (MemoryRecord.canonical_key == q) | (MemoryRecord.scope_id == q),
                )
                .order_by(MemoryRecord.importance.desc())
                .limit(5)
                .all()
            )
            out.extend(self._to_items(recs, "exact", 1.0, chain))
            if out:
                break
        return out

    def _route_fts(self, request: MemoryRecallRequest) -> list[MemoryRecallItem]:
        """PostgreSQL/SQLite text search. query genuinely participates."""
        q = request.query.strip()
        if len(q) < 2:
            return []
        tokens = _tokenize_cjk_aware(q.lower())
        if not tokens:
            return []
        chain = request.scope_context.chain()
        seen: set[str] = set()
        out: list[MemoryRecallItem] = []
        qlow = q.lower()
        for scope_type, scope_id in chain:
            recs = self._query_scope_text(scope_type, scope_id, qlow, tokens)
            for r in recs:
                if r.id in seen:
                    continue
                seen.add(r.id)
                rel = self._text_relevance(r, tokens, qlow)
                if rel <= 0:
                    continue
                out.append(self._to_item(r, "fts", rel, chain))
        out.sort(key=lambda x: x.relevance_score, reverse=True)
        return out[: self._config.automatic_recall_top_k * 2]

    def _route_vector(self, _request: MemoryRecallRequest) -> list[MemoryRecallItem]:
        """Semantic route. Adapter not wired (Qdrant) → stub returns empty."""
        if not self._config.vector_adapter_enabled:
            return []
        return []

    def _route_temporal_graph(self, _request: MemoryRecallRequest) -> list[MemoryRecallItem]:
        """Temporal / multi-hop KG route. Adapter not wired → stub returns empty."""
        if not self._config.temporal_graph_adapter_enabled:
            return []
        return []

    def _route_recent_episode(
        self, request: MemoryRecallRequest, limit: int = 3
    ) -> list[MemoryRecallItem]:
        """Query-aware fallback: recent episodic records in scope that actually
        relate to the query.

        Must NOT inject episodic memory every turn. Relevance is scored from
        query-token overlap (same scale as FTS) so the fusion threshold applies
        naturally; when nothing matches, the route returns empty and Automatic
        Recall may legitimately yield an empty pack (V2 §L2 / §762). Recent
        episodes already live in Thread Working State (history messages), so
        they must not be force-injected as long-term recall.
        """
        q = request.query.strip().lower()
        if len(q) < 2:
            return []
        tokens = _tokenize_cjk_aware(q)
        if not tokens:
            return []
        chain = request.scope_context.chain()
        seen: set[str] = set()
        out: list[MemoryRecallItem] = []
        for scope_type, scope_id in chain:
            recs = (
                self._db.query(MemoryRecord)
                .filter(
                    MemoryRecord.lifecycle_state == LifecycleState.ACTIVE.value,
                    MemoryRecord.validity_state == ValidityState.VALID.value,
                    MemoryRecord.scope_type == scope_type,
                    MemoryRecord.memory_type == "episodic",
                )
            )
            if scope_id is not None:
                recs = recs.filter(MemoryRecord.scope_id == scope_id)
            else:
                recs = recs.filter(MemoryRecord.scope_id.is_(None))
            recs = recs.order_by(MemoryRecord.observed_at.desc()).limit(limit * 3).all()
            for r in recs:
                if r.id in seen:
                    continue
                seen.add(r.id)
                rel = self._text_relevance(r, tokens, q)
                if rel <= 0:
                    continue
                out.append(self._to_item(r, "episode", rel, chain))
        out.sort(key=lambda x: x.relevance_score, reverse=True)
        return out[:limit]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _query_scope_text(
        self, scope_type: str, scope_id: str | None, qlow: str, tokens: list[str]
    ) -> Sequence[MemoryRecord]:
        q = self._db.query(MemoryRecord).filter(
            MemoryRecord.lifecycle_state == LifecycleState.ACTIVE.value,
            MemoryRecord.validity_state == ValidityState.VALID.value,
            MemoryRecord.scope_type == scope_type,
        )
        if scope_id is not None:
            q = q.filter(MemoryRecord.scope_id == scope_id)
        else:
            q = q.filter(MemoryRecord.scope_id.is_(None))
        # Match any individual token (or the full query) as a substring, so
        # token-based relevance scoring has candidates to score.
        text_conds = [or_(MemoryRecord.content.ilike(f"%{t}%"),
                           MemoryRecord.canonical_key.ilike(f"%{t}%"))
                      for t in tokens]
        text_conds.append(or_(MemoryRecord.content.ilike(f"%{qlow}%"),
                               MemoryRecord.canonical_key.ilike(f"%{qlow}%")))
        q = q.filter(or_(*text_conds))
        return q.order_by(MemoryRecord.importance.desc()).limit(
            self._config.automatic_recall_top_k * 2
        ).all()

    @staticmethod
    def _text_relevance(r: MemoryRecord, tokens: list[str], _qlow: str) -> float:
        content_low = (r.content or "").lower()
        key_low = (r.canonical_key or "").lower()
        matched = sum(1 for t in tokens if t in content_low or t in key_low)
        if matched == 0:
            return 0.0
        frac = matched / len(tokens)
        key_boost = 0.15 if any(t and t in key_low for t in tokens) else 0.0
        return min(1.0, frac * 0.8 + key_boost)

    def _to_items(
        self, recs: Sequence[MemoryRecord], route: str,
        relevance: float, chain: list[tuple[str, str | None]],
    ) -> list[MemoryRecallItem]:
        return [self._to_item(r, route, relevance, chain) for r in recs]

    def _to_item(
        self, r: MemoryRecord, route: str, relevance: float,
        chain: list[tuple[str, str | None]],
    ) -> MemoryRecallItem:
        return MemoryRecallItem(
            memory_id=r.id,
            content=r.content or "",
            memory_type=r.memory_type or "",
            canonical_key=r.canonical_key or "",
            scope_type=r.scope_type or "global",
            scope_id=r.scope_id,
            relevance_score=round(relevance, 3),
            scope_score=_scope_score(r.scope_type or "global", r.scope_id, chain),
            recency_score=_recency_score(r),
            importance_score=r.importance or 0.5,
            trust_level=r.trust_level or "semi_trusted",
            validity_state=r.validity_state or "valid",
            record_version=r.record_version or 1,
            token_cost=max(1, len(r.content or "") // 4),
            route=route,
        )
