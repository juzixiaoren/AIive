"""MemoryStore: internal memory persistence repository.

Provides structured CRUD for MemoryRecord, used exclusively by MemoryWriteService
and read-side components (MemoryReadModel).

Production write paths MUST NOT call create_record() / update_lifecycle() directly;
they must go through MemoryWriteService.

Read paths (get_active, get_by_id, get_by_key_scope, etc.) are safe for
MemoryReadModel to use.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import MemoryRecord
from aiive.memory.memory_types import MemoryProposal, LifecycleState, ValidityState


class MemoryStore:
    """Internal repository for memory_records operations.

    Write operations: called only by MemoryWriteService.
    Read operations: called by MemoryReadModel, Context Builder.
    """

    def __init__(self, db: Session) -> None:
        self._db: Session = db

    # ------------------------------------------------------------------
    # Write operations (internal — called only by MemoryWriteService)
    # ------------------------------------------------------------------

    def create_record(
        self,
        proposal: MemoryProposal,
        lifecycle_state: str = LifecycleState.CANDIDATE.value,
        validity_state: str = ValidityState.VALID.value,
        supersedes: str | None = None,
        revision_of: str | None = None,
        merged_from: list[str] | None = None,
        revision_num: int = 1,
    ) -> MemoryRecord:
        """Create a new MemoryRecord from a normalized proposal.

        Called ONLY by MemoryWriteService.
        """
        now = datetime.now(timezone.utc)
        record = MemoryRecord(
            id=str(uuid.uuid4()),
            memory_type=proposal.memory_type,
            canonical_key=proposal.canonical_key,
            cardinality=self._infer_cardinality(proposal.canonical_key),
            scope_type=proposal.scope_type,
            scope_id=proposal.scope_id,
            content=proposal.content,
            structured_value=proposal.structured_value,
            lifecycle_state=lifecycle_state,
            validity_state=validity_state,
            trust_level=proposal.trust_level,
            stability=proposal.stability,
            stability_score=proposal.stability_score,
            confidence=proposal.confidence,
            importance=proposal.importance,
            record_version=1,
            reinforce_count=0,
            source_event_id=proposal.source_event_ids[0] if proposal.source_event_ids else None,
            created_from=proposal.proposal_id,
            revision_of=revision_of,
            supersedes=supersedes,
            merged_from=merged_from,
            revision_num=revision_num,
            valid_from=now,
            observed_at=now,
            created_at=now,
            updated_at=now,
        )
        self._db.add(record)
        self._db.flush()
        return record

    def update_lifecycle(self, memory_id: str, state: str) -> None:
        """Update lifecycle_state. Called only by MemoryWriteService."""
        record = self._db.get(MemoryRecord, memory_id)
        if record:
            record.lifecycle_state = state
            record.updated_at = datetime.now(timezone.utc)
            self._db.flush()

    def update_validity(self, memory_id: str, state: str) -> None:
        """Update validity_state. Called only by MemoryWriteService."""
        record = self._db.get(MemoryRecord, memory_id)
        if record:
            record.validity_state = state
            record.updated_at = datetime.now(timezone.utc)
            self._db.flush()

    # ------------------------------------------------------------------
    # Read operations — safe for any consumer
    # ------------------------------------------------------------------

    def get_by_id(self, memory_id: str) -> MemoryRecord | None:
        """Get a single record by ID."""
        return self._db.get(MemoryRecord, memory_id)

    # Active = lifecycle='active' AND validity='valid'
    _ACTIVE_VALID: tuple[Any, Any] = (
        MemoryRecord.lifecycle_state == LifecycleState.ACTIVE.value,
        MemoryRecord.validity_state == ValidityState.VALID.value,
    )

    def get_active_valid(self) -> Sequence[MemoryRecord]:
        """Get all active+valid records (prefer scoped queries)."""
        return (
            self._db.query(MemoryRecord)
            .filter(*self._ACTIVE_VALID)
            .order_by(MemoryRecord.updated_at.desc())
            .all()
        )

    def get_active(self) -> Sequence[MemoryRecord]:
        """DEPRECATED: use get_active_valid(). Kept for backward compat."""
        return self.get_active_valid()

    def get_active_by_key(self, canonical_key: str) -> Sequence[MemoryRecord]:
        """Get active+valid records by canonical_key."""
        return (
            self._db.query(MemoryRecord)
            .filter(
                MemoryRecord.canonical_key == canonical_key,
                *self._ACTIVE_VALID,
            )
            .order_by(MemoryRecord.updated_at.desc())
            .all()
        )

    def get_active_by_key_scope(
        self, canonical_key: str, scope_type: str, scope_id: str | None
    ) -> Sequence[MemoryRecord]:
        """Get active+valid records by canonical_key + scope."""
        query = self._db.query(MemoryRecord).filter(
            MemoryRecord.canonical_key == canonical_key,
            MemoryRecord.scope_type == scope_type,
            *self._ACTIVE_VALID,
        )
        if scope_id is not None:
            query = query.filter(MemoryRecord.scope_id == scope_id)
        else:
            query = query.filter(MemoryRecord.scope_id.is_(None))
        return query.order_by(MemoryRecord.updated_at.desc()).all()

    def get_active_by_key_scope_locked(
        self, canonical_key: str, scope_type: str, scope_id: str | None
    ) -> Sequence[MemoryRecord]:
        """SELECT FOR UPDATE on active+valid records + candidates (for conflict resolution).

        Returns all records with same key+scope that are either active+valid or candidate.
        This enables candidate promotion in ConflictResolver.
        """
        query = self._db.query(MemoryRecord).filter(
            MemoryRecord.canonical_key == canonical_key,
            MemoryRecord.scope_type == scope_type,
            MemoryRecord.lifecycle_state.in_([
                LifecycleState.ACTIVE.value,
                LifecycleState.CANDIDATE.value,
            ]),
        )
        if scope_id is not None:
            query = query.filter(MemoryRecord.scope_id == scope_id)
        else:
            query = query.filter(MemoryRecord.scope_id.is_(None))
        try:
            return query.order_by(
                MemoryRecord.lifecycle_state.desc(),  # active before candidate
                MemoryRecord.updated_at.desc(),
            ).with_for_update().all()
        except Exception:
            return query.order_by(
                MemoryRecord.lifecycle_state.desc(),
                MemoryRecord.updated_at.desc(),
            ).all()

    def get_by_context_roles(
        self, roles: Sequence[str]
    ) -> Sequence[MemoryRecord]:
        """Get active+valid records by context role keys (exact + pattern prefixes).

        Query-based: matches exact canonical_keys OR canonical_key LIKE prefix%.
        No full table scan.
        """
        from sqlalchemy import or_

        from aiive.memory.memory_key_registry import get_memory_key_registry

        registry = get_memory_key_registry()
        exact_keys: set[str] = set()
        prefixes: list[str] = []
        for role in roles:
            exact_keys.update(registry.get_context_role_keys(role))
            prefixes.extend(registry.get_context_role_patterns(role))
        if not exact_keys and not prefixes:
            return []
        conditions = []
        if exact_keys:
            conditions.append(MemoryRecord.canonical_key.in_(list(exact_keys)))
        for pfx in prefixes:
            conditions.append(MemoryRecord.canonical_key.like(f"{pfx}%"))
        return (
            self._db.query(MemoryRecord)
            .filter(or_(*conditions), *self._ACTIVE_VALID)
            .order_by(MemoryRecord.updated_at.desc())
            .all()
        )

    def get_candidates_for_promotion(self, limit: int = 20) -> Sequence[MemoryRecord]:
        """Get candidate records eligible for promotion review."""
        return (
            self._db.query(MemoryRecord)
            .filter(MemoryRecord.lifecycle_state == LifecycleState.CANDIDATE.value)
            .order_by(MemoryRecord.confidence.desc())
            .limit(limit)
            .all()
        )

    def list_all(self, limit: int = 100) -> Sequence[MemoryRecord]:
        """List all records (for inspection)."""
        return (
            self._db.query(MemoryRecord)
            .order_by(MemoryRecord.updated_at.desc())
            .limit(limit)
            .all()
        )

    def db_session(self) -> Session:
        """Return the raw DB session for read-side components."""
        return self._db

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_cardinality(canonical_key: str) -> str:
        """Quick cardinality check without full registry resolution."""
        from aiive.memory.memory_key_registry import get_memory_key_registry
        registry = get_memory_key_registry()
        if registry.is_single_cardinality(canonical_key):
            return "single"
        return "multi"
