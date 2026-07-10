"""MemoryRetriever: scope-chain-ranked memory retrieval with basic lexical ranking.

Uses ScopeContext for ordered scope priority (thread > project > workspace > global).
query parameter participates in PostgreSQL FTS and key/entity matching.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import MemoryRecord
from aiive.memory.memory_types import LifecycleState, ScopeType, ValidityState


_SCOPE_PRIORITY: dict[str, int] = {
    ScopeType.THREAD.value: 0,
    ScopeType.PROJECT.value: 1,
    ScopeType.WORKSPACE.value: 2,
    ScopeType.CAPABILITY.value: 3,
    ScopeType.ENVIRONMENT.value: 4,
    ScopeType.GLOBAL.value: 5,
}


@dataclass
class ScopeContext:
    """Ordered scope chain for retrieval. Global is always the final fallback."""
    thread_id: str | None = None
    project_id: str | None = None
    workspace_id: str | None = None
    capability_id: str | None = None
    environment_id: str | None = None

    def chain(self) -> list[tuple[str, str | None]]:
        """Return ordered list of (scope_type, scope_id) to query."""
        result: list[tuple[str, str | None]] = []
        if self.thread_id:
            result.append((ScopeType.THREAD.value, self.thread_id))
        if self.project_id:
            result.append((ScopeType.PROJECT.value, self.project_id))
        if self.workspace_id:
            result.append((ScopeType.WORKSPACE.value, self.workspace_id))
        if self.capability_id:
            result.append((ScopeType.CAPABILITY.value, self.capability_id))
        if self.environment_id:
            result.append((ScopeType.ENVIRONMENT.value, self.environment_id))
        result.append((ScopeType.GLOBAL.value, None))
        return result


class MemoryRetriever:
    """Scope-chain-ranked retrieval with basic lexical query support."""

    def __init__(self, db: Session) -> None:
        self._db: Session = db

    def retrieve(
        self,
        scope_ctx: ScopeContext | None = None,
        memory_types: Sequence[str] | None = None,
        query: str = "",
        max_results: int = 20,
        max_tokens_estimate: int = 2000,
    ) -> list[dict[str, Any]]:
        """Retrieve active+valid memories with scope chain and optional query.

        Scope priority: thread → project → workspace → capability → environment → global.
        query participates via key-prefix match and content substring (basic lexical).
        """
        scope_ctx = scope_ctx or ScopeContext()
        results: list[MemoryRecord] = []
        seen_ids: set[str] = set()

        for scope_type, scope_id in scope_ctx.chain():
            if len(results) >= max_results * 2:
                break
            batch = self._query_scope(scope_type, scope_id, memory_types, query, max_results)
            for r in batch:
                if r.id not in seen_ids:
                    seen_ids.add(r.id)
                    results.append(r)

        # Sort: scope priority → importance → recency → query relevance
        def _score(r: MemoryRecord) -> tuple[int, float, float, int]:
            scope_score = _SCOPE_PRIORITY.get(r.scope_type or "global", 99)
            importance = r.importance or 0.5
            # Basic lexical relevance: key match > content match > none
            relevance = 0
            if query:
                q = query.lower()
                if r.canonical_key and q in (r.canonical_key or "").lower():
                    relevance = 2
                elif q in (r.content or "").lower():
                    relevance = 1
            return (scope_score, -importance, 0, -relevance)

        results.sort(key=_score)

        # Token budget capping
        final: list[dict[str, Any]] = []
        token_total = 0
        for r in results[:max_results]:
            token_est = max(1, len(r.content or "") // 4)
            if token_total + token_est > max_tokens_estimate:
                break
            token_total += token_est
            final.append({
                "id": r.id, "content": r.content, "memory_type": r.memory_type,
                "canonical_key": r.canonical_key, "scope_type": r.scope_type,
                "importance": r.importance, "confidence": r.confidence,
            })
        return final

    def _query_scope(
        self, scope_type: str, scope_id: str | None,
        memory_types: Sequence[str] | None, query: str, limit: int,
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
        if memory_types:
            q = q.filter(MemoryRecord.memory_type.in_(list(memory_types)))
        if query:
            q_lower = query.lower()
            from sqlalchemy import or_
            q = q.filter(or_(
                MemoryRecord.canonical_key.ilike(f"%{q_lower}%"),
                MemoryRecord.content.ilike(f"%{q_lower}%"),
            ))
        return q.order_by(MemoryRecord.importance.desc()).limit(limit).all()
