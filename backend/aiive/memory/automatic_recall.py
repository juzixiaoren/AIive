"""Automatic Recall 引擎：每轮执行 query-aware 的多路召回。

当前生产路由：
  1. Exact / Structured —— canonical_key / entity / scope-id 精确匹配
  2. Lexical            —— query 参与 ILIKE 与 token overlap 匹配
  3. Recent Episode     —— scope 内与 query 相关的近期 episodic 记录

各路由均执行 scope 约束的 SQL，不先全量读取再在 Python 中过滤。候选统一由
`recall_fusion.fuse_and_pack` 融合。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Protocol

# ---------------------------------------------------------------------------
# CJK-aware tokenization — avoids the \W+ pitfall where Chinese text
# (all word-characters under Python 3 re.UNICODE) sticks together as a
# single token. Overlapping bigrams from CJK runs are appended to the
# standard \W+ token list so character-level matching works.
# ---------------------------------------------------------------------------

_CJK_RE = re.compile("[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]+")


def tokenize_cjk_aware(text: str, min_len: int = 2) -> list[str]:
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

from sqlalchemy import cast, or_, Text
from sqlalchemy.orm import Session

from aiive.db.models import MemoryRecord
from aiive.memory.memory_policy import MemoryPolicyEngine, MemoryReadChannel
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

logger = logging.getLogger(__name__)

_KEYISH = re.compile(r"^[A-Za-z0-9_.]+$")


class VectorSearchService(Protocol):
    """Automatic Recall 所需的最小向量检索接口。"""

    def search(
        self,
        request: MemoryRecallRequest,
        *,
        include_sleeping: bool,
        include_archived: bool,
        limit: int,
    ) -> Sequence[tuple[MemoryRecord, float]]: ...


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

    def __init__(
        self,
        db: Session,
        config: RecallConfig | None = None,
        vector_service: VectorSearchService | None = None,
    ) -> None:
        self._db: Session = db
        self._store: MemoryStore = MemoryStore(db)
        self._policy: MemoryPolicyEngine = MemoryPolicyEngine()
        self._config: RecallConfig = config or RecallConfig()
        self._vector_service: VectorSearchService | None = vector_service
        if self._vector_service is None:
            from aiive.config import settings
            if settings.aiive_memory_vector_enabled:
                from aiive.memory.vector_projection import MemoryVectorProjectionService
                self._vector_service = MemoryVectorProjectionService(db)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def recall(
        self, request: MemoryRecallRequest, include_sleeping: bool = False,
        include_archived: bool = False,
    ) -> tuple[MemoryRecallPack, list[RecallCandidateTrace]]:
        """执行全部启用的召回路由并融合为一个 pack。

        include_sleeping=True 时（J.5「扩大召回/高相关」），exact / lexical
        高相关路由放宽到 `lifecycle_state IN (active, sleeping)`；episode 兜底路由
        始终仅 active，避免陈旧、低价值的情节记忆被重新带回上下文。
        include_archived=True 时追加 archived（仅经统一检索显式请求，不自动
        wake），且 validity 放宽为 IN (valid, expired) —— archived 记录的
        validity 通常已是 expired，仅追加 lifecycle 状态无法命中；superseded
        仍排除（已被新记录取代）。

        注意：vector 路由的 include_sleeping / include_archived 仅为接口透传。
        向量投影按现状设计只为 active+valid 记录维护向量（sleeping / archived
        时删除向量），因此该路由实际只返回 active+valid 结果。
        """
        candidates: list[MemoryRecallItem] = []
        candidates += self._route_exact(request, include_sleeping, include_archived)
        candidates += self._route_lexical(request, include_sleeping, include_archived)
        try:
            candidates += self._route_vector(request, include_sleeping, include_archived)
        except Exception:
            logger.exception("向量召回失败，降级为 exact / lexical / episode")
        candidates += self._route_recent_episode(request)
        candidates = [item for item in candidates if item.content]
        return fuse_and_pack(candidates, request, self._config)

    @staticmethod
    def _lifecycle_states(
        include_sleeping: bool, include_archived: bool = False,
    ) -> list[str]:
        """返回参与召回的 lifecycle_state 取值列表。

        默认仅 active；include_sleeping=True 时追加 sleeping（J.5）；
        include_archived=True 时追加 archived（统一检索显式请求，不自动 wake）。
        forgotten / superseded / expired / candidate 永不进入普通召回。
        """
        states = [LifecycleState.ACTIVE.value]
        if include_sleeping:
            states.append(LifecycleState.SLEEPING.value)
        if include_archived:
            states.append(LifecycleState.ARCHIVED.value)
        return states

    @staticmethod
    def _validity_states(include_archived: bool = False) -> list[str]:
        """返回参与召回的 validity_state 取值列表。

        默认仅 valid；include_archived=True 时放宽为 (valid, expired)：
        archived 记录的 validity 通常已被置为 expired（archive 动作副作用），
        若仍恒过滤 valid，追加 archived lifecycle 将永远命中不到记录。
        superseded 恒排除（已被新记录取代，召回新记录即可）。
        """
        states = [ValidityState.VALID.value]
        if include_archived:
            states.append(ValidityState.EXPIRED.value)
        return states

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    def _route_exact(
        self, request: MemoryRecallRequest, include_sleeping: bool = False,
        include_archived: bool = False,
    ) -> list[MemoryRecallItem]:
        """Match canonical_key / scope-id precisely. High precision → passes threshold."""
        q = request.query.strip()
        if not q:
            return []
        chain = request.scope_context.chain()
        if not _KEYISH.match(q):
            return []
        states = self._lifecycle_states(include_sleeping, include_archived)
        validity = self._validity_states(include_archived)
        out: list[MemoryRecallItem] = []
        for scope_type, scope_id in chain:
            query = (
                self._db.query(MemoryRecord)
                .filter(
                    MemoryRecord.lifecycle_state.in_(states),
                    MemoryRecord.validity_state.in_(validity),
                    MemoryRecord.scope_type == scope_type,
                    (MemoryRecord.canonical_key == q) | (MemoryRecord.scope_id == q),
                )
            )
            # scope_id 必须与 chain 中的取值一致（与 _route_lexical 相同），
            # 否则仅按 scope_type 过滤会把其他 thread/project 的记忆跨界召回。
            if scope_id is not None:
                query = query.filter(MemoryRecord.scope_id == scope_id)
            else:
                query = query.filter(MemoryRecord.scope_id.is_(None))
            recs = query.order_by(MemoryRecord.importance.desc()).limit(5).all()
            out.extend(self._to_items(recs, "exact", 1.0, chain))
            if out:
                break
        return out

    def _route_lexical(
        self, request: MemoryRecallRequest, include_sleeping: bool = False,
        include_archived: bool = False,
    ) -> list[MemoryRecallItem]:
        """执行 PostgreSQL/SQLite 可移植的词汇检索，query 真实参与匹配。"""
        q = request.query.strip()
        if len(q) < 2:
            return []
        tokens = tokenize_cjk_aware(q.lower())
        if not tokens:
            return []
        chain = request.scope_context.chain()
        seen: set[str] = set()
        out: list[MemoryRecallItem] = []
        qlow = q.lower()
        for scope_type, scope_id in chain:
            recs = self._query_scope_text(
                scope_type, scope_id, qlow, tokens, include_sleeping, include_archived,
            )
            for r in recs:
                if r.id in seen:
                    continue
                seen.add(r.id)
                rel = self._text_relevance(r, tokens, qlow)
                if rel <= 0:
                    continue
                item = self._to_item(r, "lexical", rel, chain)
                if item is not None:
                    out.append(item)
        out.sort(key=lambda x: x.relevance_score, reverse=True)
        return out[: self._config.automatic_recall_top_k * 2]

    def _route_vector(
        self,
        request: MemoryRecallRequest,
        include_sleeping: bool = False,
        include_archived: bool = False,
    ) -> list[MemoryRecallItem]:
        """通过已配置的 pgvector 服务执行语义召回。"""
        if self._vector_service is None:
            return []
        from aiive.config import settings
        rows = self._vector_service.search(
            request,
            include_sleeping=include_sleeping,
            include_archived=include_archived,
            limit=min(self._config.vector_top_k, settings.aiive_memory_vector_top_k),
        )
        chain = request.scope_context.chain()
        out: list[MemoryRecallItem] = []
        for record, relevance in rows:
            if relevance <= 0:
                continue
            item = self._to_item(record, "vector", relevance, chain)
            if item is not None:
                out.append(item)
        return out

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
        tokens = tokenize_cjk_aware(q)
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
                item = self._to_item(r, "episode", rel, chain)
                if item is not None:
                    out.append(item)
        out.sort(key=lambda x: x.relevance_score, reverse=True)
        return out[:limit]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _query_scope_text(
        self, scope_type: str, scope_id: str | None, qlow: str, tokens: list[str],
        include_sleeping: bool = False, include_archived: bool = False,
    ) -> Sequence[MemoryRecord]:
        q = self._db.query(MemoryRecord).filter(
            MemoryRecord.lifecycle_state.in_(
                self._lifecycle_states(include_sleeping, include_archived),
            ),
            MemoryRecord.validity_state.in_(self._validity_states(include_archived)),
            MemoryRecord.scope_type == scope_type,
        )
        if scope_id is not None:
            q = q.filter(MemoryRecord.scope_id == scope_id)
        else:
            q = q.filter(MemoryRecord.scope_id.is_(None))
        # Match any individual token (or the full query) as a substring, so
        # token-based relevance scoring has candidates to score. keywords(JSON)
        # 是显式检索词，cast 成 Text 后做 ILIKE 子串匹配，使「奶茶」能命中带
        # ['奶茶','霸王茶姬'] 关键词的记忆。
        text_conds = [or_(MemoryRecord.content.ilike(f"%{t}%"),
                           MemoryRecord.canonical_key.ilike(f"%{t}%"),
                           cast(MemoryRecord.keywords, Text).ilike(f"%{t}%"))
                      for t in tokens]
        text_conds.append(or_(MemoryRecord.content.ilike(f"%{qlow}%"),
                               MemoryRecord.canonical_key.ilike(f"%{qlow}%"),
                               cast(MemoryRecord.keywords, Text).ilike(f"%{qlow}%")))
        q = q.filter(or_(*text_conds))
        return q.order_by(MemoryRecord.importance.desc()).limit(
            self._config.automatic_recall_top_k * 2
        ).all()

    @staticmethod
    def _text_relevance(r: MemoryRecord, tokens: list[str], _qlow: str) -> float:
        content_low = (r.content or "").lower()
        key_low = (r.canonical_key or "").lower()
        kw_lows = [k.lower() for k in (r.keywords or [])]
        matched = 0
        for t in tokens:
            if t in content_low or t in key_low:
                matched += 1
                continue
            # 关键词双向命中：token 包含关键词、或关键词包含 token（同义/上位）
            if any(t in kw or kw in t for kw in kw_lows):
                matched += 1
        if matched == 0:
            return 0.0
        frac = matched / len(tokens)
        key_boost = 0.15 if any(t and t in key_low for t in tokens) else 0.0
        return min(1.0, frac * 0.8 + key_boost)

    def _to_items(
        self, recs: Sequence[MemoryRecord], route: str,
        relevance: float, chain: list[tuple[str, str | None]],
    ) -> list[MemoryRecallItem]:
        items = [self._to_item(r, route, relevance, chain) for r in recs]
        return [item for item in items if item is not None]

    def _to_item(
        self, r: MemoryRecord, route: str, relevance: float,
        chain: list[tuple[str, str | None]],
    ) -> MemoryRecallItem | None:
        content = self._policy.render_content(
            r.content or "", r.sensitivity, MemoryReadChannel.LLM_CONTEXT,
        )
        if content is None:
            return None
        return MemoryRecallItem(
            memory_id=r.id,
            content=content,
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
            lifecycle_state=r.lifecycle_state or "",
        )
