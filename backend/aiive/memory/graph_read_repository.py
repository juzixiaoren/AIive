"""Authorized graph peers: hidden sources must not influence conflict metadata."""

from __future__ import annotations
from collections.abc import Sequence
from sqlalchemy.orm import Session
from aiive.db.models import GraphRelation, MemoryRecord
from aiive.forget.visibility_service import ForgetVisibilityService
from aiive.memory.memory_policy import MemoryPolicyEngine, MemoryReadChannel
from aiive.memory.graph_types import canonical_relation_fact


class GraphReadRepository:
    def __init__(self, db: Session) -> None:
        self.db: Session = db

    def current_peers(
        self,
        subject_ids: set[str],
        scope_chain: Sequence[tuple[str, str | None]],
        limit: int = 1000,
        channel: MemoryReadChannel = MemoryReadChannel.LLM_CONTEXT,
    ) -> list[GraphRelation]:
        if not subject_ids:
            return []
        rows = (
            self.db.query(GraphRelation)
            .filter(
                GraphRelation.status == "active",
                GraphRelation.subject_entity_id.in_(subject_ids),
            )
            .order_by(GraphRelation.id)
            .limit(limit)
            .all()
        )
        ids = {r.source_memory_id for r in rows}
        records = {
            r.id: r
            for r in self.db.query(MemoryRecord).filter(MemoryRecord.id.in_(ids)).populate_existing().all()
        }
        allowed = set(ForgetVisibilityService.filter_memory_ids(self.db, list(ids)))
        result = []
        for row in rows:
            record = records.get(row.source_memory_id)
            if record is None or record.id not in allowed:
                continue
            if record.lifecycle_state != "active" or record.validity_state != "valid":
                continue
            if (record.scope_type, record.scope_id) not in scope_chain or (
                row.scope_type,
                row.scope_id,
            ) != (record.scope_type, record.scope_id):
                continue
            if (
                not MemoryPolicyEngine()
                .decide_read(record.sensitivity, channel)
                .allowed
            ):
                continue
            if (
                canonical_relation_fact(record.graph_facts, record.id, row.relation_key)
                is None
            ):
                continue
            result.append(row)
        return result
