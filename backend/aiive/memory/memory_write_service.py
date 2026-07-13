"""MemoryWriteService: unified transactional memory write pipeline.

THE ONLY entry point for all memory writes. Handles:
1. Advisory lock acquisition (per canonical_key + scope)
2. SELECT FOR UPDATE on existing active records
3. Conflict resolution (via ConflictResolver)
4. Atomic write: memory_records + evidence + lineage + proposal + event + outbox
5. Strong-consistency boundary: PostgreSQL transaction only
6. Outbox enqueue for async projection (KG/vector/markdown/cache)

All creates, reinforces, revises, supersedes, merges, sleeps, archives, forgets
MUST go through this service. No other module writes directly to MemoryStore.
"""

from __future__ import annotations

import hashlib
import logging
import struct
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from aiive.context.run_context import RunContext
from aiive.db.models import (
    MemoryEvidence,
    MemoryLineage,
    MemoryProposal as MemoryProposalModel,
    MemoryRecord,
    OutboxJob,
)
from aiive.memory.conflict_resolver import ConflictResolver, ResolutionResult
from aiive.memory.memory_gate import GateDecision, MemoryGate
from aiive.memory.memory_key_registry import MemoryKeyRegistry, get_memory_key_registry
from aiive.memory.memory_store import MemoryStore
from aiive.memory.memory_types import (
    EvidenceItem,
    LifecycleState,
    LineageOperation,
    MemoryProposal,
    TrustLevel,
    ValidityState,
)
from aiive.runtime.event_logger import EventLogger

logger = logging.getLogger(__name__)


# ============================================================================
# Write result
# ============================================================================

class WriteResult:
    """Result of a single MemoryWriteService.write() call."""

    written: bool
    operation: str
    memory_id: str
    state: str
    reason: str
    superseded_ids: list[str]

    def __init__(
        self,
        written: bool,
        operation: str = "",
        memory_id: str = "",
        state: str = "",
        reason: str = "",
        superseded_ids: list[str] | None = None,
    ) -> None:
        self.written = written
        self.operation = operation
        self.memory_id = memory_id
        self.state = state
        self.reason = reason
        self.superseded_ids = superseded_ids or []

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.written,
            "operation": self.operation,
            "memory_id": self.memory_id,
            "state": self.state,
            "reason": self.reason,
            "superseded_ids": self.superseded_ids,
        }


# ============================================================================
# MemoryWriteService
# ============================================================================

class MemoryWriteService:
    """Unified transactional memory write service.

    Strong consistency boundary: PostgreSQL transaction.
    Async projections (KG/vector/markdown/cache) are enqueued via Outbox.
    """

    def __init__(
        self,
        db: Session,
        registry: MemoryKeyRegistry | None = None,
    ) -> None:
        self._db: Session = db
        self._store: MemoryStore = MemoryStore(db)
        self._gate: MemoryGate = MemoryGate(registry)
        self._resolver: ConflictResolver = ConflictResolver(registry)
        self._logger: EventLogger = EventLogger(db)
        self._registry: MemoryKeyRegistry = registry or get_memory_key_registry()

    # ------------------------------------------------------------------
    # Public API: write a normalized proposal
    # ------------------------------------------------------------------

    def write(
        self,
        proposal: MemoryProposal,
        run_context: RunContext | None = None,
        _enqueue_projections: bool = True,
    ) -> WriteResult:
        """Write a normalized MemoryProposal through the full pipeline.

        LLM extraction must happen OUTSIDE the database transaction.
        This method only does deterministic gate/resolve/persist.
        """
        if run_context is None or not run_context.thread_id:
            raise RuntimeError("MemoryWriteService.write() requires valid RunContext")
        tid: str = run_context.thread_id

        # 1. Gate admission
        gate_decision: GateDecision = self._gate.decide(proposal)
        if gate_decision.decision == "reject":
            self._persist_proposal(proposal, gate_decision, final_op="reject")
            return WriteResult(
                written=False,
                reason=f"Gate rejected: {gate_decision.reason}",
            )

        # 2. Acquire advisory lock (hash of canonical_key + scope)
        lock_id: int = self._compute_lock_id(
            proposal.canonical_key, proposal.scope_type, proposal.scope_id
        )
        self._acquire_lock(lock_id)

        try:
            # 3. SELECT FOR UPDATE existing active records
            existing_active: Sequence[MemoryRecord] = (
                self._store.get_active_by_key_scope_locked(
                    proposal.canonical_key,
                    proposal.scope_type,
                    proposal.scope_id,
                )
            )

            # 4. Conflict resolution
            resolution: ResolutionResult = self._resolver.resolve(
                proposal, existing_active
            )

            # 5. Execute the operation
            result: WriteResult
            if resolution.operation == "reinforce":
                result = self._execute_reinforce(
                    proposal, resolution.existing_record or existing_active[0],
                    gate_decision, tid,
                )
            elif resolution.operation in ("supersede", "revise"):
                result = self._execute_supersede_or_revise(
                    proposal, resolution, gate_decision, tid,
                )
            elif resolution.operation == "promote":
                result = self._execute_promote_candidate(
                    proposal, resolution, gate_decision, tid,
                )
            elif resolution.operation == "merge":
                result = self._execute_merge(
                    proposal, resolution, gate_decision, tid,
                )
            elif resolution.operation == "ignore":
                self._persist_proposal(proposal, gate_decision, final_op="ignore")
                result = WriteResult(
                    written=False,
                    reason=resolution.reason,
                )
            else:
                # create (including candidate from Gate)
                target_state: str = (
                    LifecycleState.CANDIDATE.value
                    if gate_decision.decision == "candidate"
                    else LifecycleState.ACTIVE.value
                )
                record: MemoryRecord = self._store.create_record(
                    proposal=proposal,
                    lifecycle_state=target_state,
                    validity_state=ValidityState.VALID.value,
                )
                self._write_evidence_batch(record.id, proposal.evidence)
                self._persist_proposal(
                    proposal, gate_decision,
                    final_op="create", final_memory_id=record.id,
                )
                self._log_event(tid, proposal, "memory.created", record.id)
                self._enqueue_projection(record, "memory.created")
                result = WriteResult(
                    written=True,
                    operation="create",
                    memory_id=record.id,
                    state=target_state,
                )

            return result
        finally:
            self._release_lock(lock_id)

    # ------------------------------------------------------------------
    # Maintenance operations (sleep, archive, wake)
    # ------------------------------------------------------------------

    def execute_maintenance(
        self,
        proposal: MemoryProposal,
        run_context: RunContext,
    ) -> WriteResult:
        """Execute a maintenance proposal (sleep/archive/wake) on an existing record."""
        tid: str = run_context.thread_id
        target_memory_id: str = proposal.source_event_ids[0] if proposal.source_event_ids else ""

        if not target_memory_id:
            return WriteResult(written=False, reason="No target memory_id in maintenance proposal")

        record: MemoryRecord | None = self._store.get_by_id(target_memory_id)
        if record is None:
            return WriteResult(written=False, reason=f"Memory {target_memory_id} not found")

        op: str = proposal.proposed_operation

        if op == "sleep":
            self._store.update_lifecycle(record.id, LifecycleState.SLEEPING.value)
            self._log_event(tid, proposal, "memory.sleep", record.id)
            self._enqueue_projection(record, "memory.sleep", invalidate_cache=True)
            return WriteResult(written=True, operation="sleep", memory_id=record.id)

        if op == "archive":
            self._store.update_lifecycle(record.id, LifecycleState.ARCHIVED.value)
            self._store.update_validity(record.id, ValidityState.EXPIRED.value)
            self._log_event(tid, proposal, "memory.archived", record.id)
            self._enqueue_projection(record, "memory.archived", invalidate_cache=True)
            return WriteResult(written=True, operation="archive", memory_id=record.id)

        if op == "wake":
            self._store.update_lifecycle(record.id, LifecycleState.ACTIVE.value)
            self._log_event(tid, proposal, "memory.wake", record.id)
            self._enqueue_projection(record, "memory.wake")
            return WriteResult(written=True, operation="wake", memory_id=record.id)

        return WriteResult(written=False, reason=f"Unknown maintenance operation: {op}")

    # ------------------------------------------------------------------
    # Forget (Saga — covers all layers)
    # ------------------------------------------------------------------

    def forget(
        self,
        memory_id: str,
        reason: str = "",
        run_context: RunContext | None = None,
    ) -> WriteResult:
        """Execute forget Saga: irreversibly remove all content and evidence.

        Covers: memory_records, evidence, event (tombstone), outbox (projection cleanup).
        """
        record: MemoryRecord | None = self._store.get_by_id(memory_id)
        if record is None:
            return WriteResult(written=False, reason="Memory not found")

        tombstone: str = f"forgotten:{memory_id[:8]}:{datetime.now(timezone.utc).isoformat()}"

        # 1. Scrub content
        record.lifecycle_state = LifecycleState.FORGOTTEN.value
        record.validity_state = ValidityState.SUPERSEDED.value
        record.content = tombstone
        record.structured_value = None
        record.updated_at = datetime.now(timezone.utc)

        # 2. Delete evidence (CASCADE handled by FK, or manual cleanup)
        self._db.query(MemoryEvidence).filter(
            MemoryEvidence.memory_id == memory_id
        ).delete()

        # 3. Record tombstone event
        if run_context and run_context.thread_id:
            self._logger.log_event(
                trace_id=run_context.trace_id,
                thread_id=run_context.thread_id,
                event_type="memory.forgotten",
                payload={
                    "memory_id": memory_id,
                    "reason": reason,
                    "tombstone": tombstone,
                },
            )

        # 4. Enqueue projection cleanup
        self._enqueue_projection(record, "memory.forgotten", invalidate_cache=True)

        # 5. Persist ForgetRequest audit
        from aiive.db.models import ForgetRequest

        fr = ForgetRequest(memory_id=memory_id, reason=reason, tombstone=tombstone)
        self._db.add(fr)

        return WriteResult(
            written=True,
            operation="forget",
            memory_id=memory_id,
        )

    # ------------------------------------------------------------------
    # Promotion: candidate → active
    # ------------------------------------------------------------------

    def promote(self, memory_id: str, run_context: RunContext) -> WriteResult:
        """Promote a candidate record to active (with lineage)."""
        record: MemoryRecord | None = self._store.get_by_id(memory_id)
        if record is None:
            return WriteResult(written=False, reason="Memory not found")
        if record.lifecycle_state != LifecycleState.CANDIDATE.value:
            return WriteResult(
                written=False,
                reason=f"Cannot promote: current state is {record.lifecycle_state}",
            )

        # Write lineage for promote
        self._db.add(MemoryLineage(
            predecessor_id=record.id,
            successor_id=record.id,
            operation=LineageOperation.PROMOTE.value,
            reason="Candidate promoted to active",
        ))

        record.lifecycle_state = LifecycleState.ACTIVE.value
        record.observed_at = datetime.now(timezone.utc)
        record.record_version += 1
        record.updated_at = datetime.now(timezone.utc)

        self._log_event(
            run_context.thread_id,
            MemoryProposal(memory_type=record.memory_type, canonical_key=record.canonical_key or "", content=""),
            "memory.promoted",
            record.id,
        )

        return WriteResult(written=True, operation="promote", memory_id=record.id)

    # ------------------------------------------------------------------
    # Internal execution helpers
    # ------------------------------------------------------------------

    def _execute_promote_candidate(
        self,
        proposal: MemoryProposal,
        resolution: ResolutionResult,
        gate_decision: GateDecision,
        thread_id: str,
    ) -> WriteResult:
        """Promote candidate → active with lineage."""
        candidate = resolution.candidate_record
        if candidate is None:
            return WriteResult(written=False, reason="No candidate to promote")

        self._db.add(MemoryLineage(
            predecessor_id=candidate.id,
            successor_id=candidate.id,
            operation=LineageOperation.PROMOTE.value,
            reason=resolution.reason,
        ))

        candidate.lifecycle_state = LifecycleState.ACTIVE.value
        candidate.confidence = min(1.0, (candidate.confidence or 0.5) + 0.1)
        candidate.observed_at = datetime.now(timezone.utc)
        candidate.record_version += 1
        candidate.updated_at = datetime.now(timezone.utc)

        self._write_evidence_batch(candidate.id, proposal.evidence)
        self._persist_proposal(
            proposal, gate_decision,
            final_op="promote", final_memory_id=candidate.id,
        )
        self._log_event(thread_id, proposal, "memory.promoted", candidate.id)
        self._enqueue_projection(candidate, "memory.promoted")

        return WriteResult(
            written=True, operation="promote",
            memory_id=candidate.id, state=LifecycleState.ACTIVE.value,
        )

    def _execute_reinforce(
        self,
        proposal: MemoryProposal,
        existing: MemoryRecord,
        gate_decision: GateDecision,
        thread_id: str,
    ) -> WriteResult:
        """Reinforce: update existing record. Skip if source_event already exists."""
        # 检查 source_event 是否已被计算过（防重复强化）
        new_event_ids = set(proposal.source_event_ids or [])
        if new_event_ids:
            existing_event_ids = {
                e.source_event_id for e in
                self._db.query(MemoryEvidence).filter(
                    MemoryEvidence.memory_id == existing.id,
                    MemoryEvidence.source_event_id.in_(list(new_event_ids)),
                ).all()
            }
            new_count = len(new_event_ids - existing_event_ids)
            if new_count == 0:
                # 所有 source_event 已存在 → 跳过强化
                self._persist_proposal(
                    proposal, gate_decision,
                    final_op="ignore", final_memory_id=existing.id,
                )
                return WriteResult(
                    written=False, operation="ignore",
                    reason="All source_events already counted",
                )
        else:
            new_count = 1

        # 基于独立 evidence 数量更新置信度
        trust_mult = 0.1 if proposal.trust_level == TrustLevel.TRUSTED.value else 0.05
        existing.confidence = min(1.0, (existing.confidence or 0.5) + trust_mult * new_count)
        existing.reinforce_count = (existing.reinforce_count or 0) + new_count
        existing.last_reinforced_at = datetime.now(timezone.utc)
        existing.observed_at = datetime.now(timezone.utc)
        existing.record_version += 1
        existing.updated_at = datetime.now(timezone.utc)

        self._write_evidence_batch(existing.id, proposal.evidence)
        self._persist_proposal(
            proposal, gate_decision,
            final_op="reinforce", final_memory_id=existing.id,
        )
        self._log_event(thread_id, proposal, "memory.reinforced", existing.id)
        self._enqueue_projection(existing, "memory.reinforced", invalidate_cache=True)

        return WriteResult(
            written=True, operation="reinforce",
            memory_id=existing.id, state=LifecycleState.ACTIVE.value,
        )

    def _execute_supersede_or_revise(
        self,
        proposal: MemoryProposal,
        resolution: ResolutionResult,
        gate_decision: GateDecision,
        thread_id: str,
    ) -> WriteResult:
        """Supersede or revise: create new record, mark old as superseded, write lineage."""
        old: MemoryRecord | None = resolution.existing_record
        if old is None:
            return WriteResult(written=False, reason="No existing record to supersede/revise")

        # Mark old validity as superseded, keep lifecycle as-is
        old.validity_state = ValidityState.SUPERSEDED.value
        old.updated_at = datetime.now(timezone.utc)

        # Create new
        new_record: MemoryRecord = self._store.create_record(
            proposal=proposal,
            lifecycle_state=LifecycleState.ACTIVE.value,
            validity_state=ValidityState.VALID.value,
            supersedes=old.id,
            revision_of=old.id if resolution.operation == "revise" else None,
            revision_num=(old.revision_num or 1) + 1,
        )

        # Bidirectional link
        old.superseded_by = new_record.id

        # Lineage
        self._db.add(MemoryLineage(
            predecessor_id=old.id,
            successor_id=new_record.id,
            operation=resolution.operation,
            reason=resolution.reason,
        ))

        self._write_evidence_batch(new_record.id, proposal.evidence)
        self._persist_proposal(
            proposal, gate_decision,
            final_op=resolution.operation, final_memory_id=new_record.id,
        )
        self._log_event(thread_id, proposal, "memory.superseded", new_record.id)
        self._enqueue_projection(new_record, "memory.superseded", invalidate_cache=True)

        return WriteResult(
            written=True,
            operation=resolution.operation,
            memory_id=new_record.id,
            state=LifecycleState.ACTIVE.value,
            superseded_ids=[old.id],
        )

    def _execute_merge(
        self,
        proposal: MemoryProposal,
        resolution: ResolutionResult,
        gate_decision: GateDecision,
        thread_id: str,
    ) -> WriteResult:
        """Merge multiple existing records into one new record."""
        old_ids: list[str] = [r.id for r in resolution.existing_records]

        for old in resolution.existing_records:
            old.validity_state = ValidityState.SUPERSEDED.value
            old.updated_at = datetime.now(timezone.utc)

        new_record: MemoryRecord = self._store.create_record(
            proposal=proposal,
            lifecycle_state=LifecycleState.ACTIVE.value,
            validity_state=ValidityState.VALID.value,
            merged_from=old_ids,
        )

        # Update bidirectional links
        for old in resolution.existing_records:
            old.superseded_by = new_record.id
            self._db.add(MemoryLineage(
                predecessor_id=old.id,
                successor_id=new_record.id,
                operation=LineageOperation.MERGE.value,
                reason=resolution.reason,
            ))

        self._write_evidence_batch(new_record.id, proposal.evidence)
        self._persist_proposal(
            proposal, gate_decision,
            final_op="merge", final_memory_id=new_record.id,
        )
        self._log_event(thread_id, proposal, "memory.merged", new_record.id)
        self._enqueue_projection(new_record, "memory.merged", invalidate_cache=True)

        return WriteResult(
            written=True,
            operation="merge",
            memory_id=new_record.id,
            state=LifecycleState.ACTIVE.value,
            superseded_ids=old_ids,
        )

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _persist_proposal(
        self,
        proposal: MemoryProposal,
        gate_decision: GateDecision,
        final_op: str,
        final_memory_id: str = "",
    ) -> None:
        """Persist a MemoryProposal to the memory_proposals table.

        Checks idempotency_key before insert to safely handle retries
        and batch deduplication without raising UniqueViolation.
        """
        ikey: str = proposal.idempotency_key
        if ikey:
            existing = self._db.query(MemoryProposalModel).filter(
                MemoryProposalModel.idempotency_key == ikey
            ).first()
            if existing is not None:
                logger.debug(
                    "Proposal idempotency_key already exists, skipping: %s", ikey
                )
                return

        mp = MemoryProposalModel(
            proposal_id=proposal.proposal_id,
            source_event_ids=proposal.source_event_ids,
            memory_type=proposal.memory_type,
            canonical_key=proposal.canonical_key,
            scope_type=proposal.scope_type,
            scope_id=proposal.scope_id,
            content=proposal.content,
            structured_value=proposal.structured_value,
            evidence=[e.model_dump() for e in proposal.evidence],
            trust_level=proposal.trust_level,
            confidence=proposal.confidence,
            importance=proposal.importance,
            stability=proposal.stability,
            stability_score=proposal.stability_score,
            proposed_operation=proposal.proposed_operation,
            gate_decision=gate_decision.decision,
            gate_reason=gate_decision.reason,
            blocked_reason=gate_decision.blocked_reason,
            final_operation=final_op,
            final_memory_id=final_memory_id or None,
            requires_confirmation=gate_decision.requires_confirmation,
            extractor_name=proposal.extractor_name,
            extractor_version=proposal.extractor_version,
            idempotency_key=proposal.idempotency_key,
            raw_payload=proposal.raw_payload,
            normalized_payload=proposal.normalized_payload,
        )
        self._db.add(mp)

    def _write_evidence_batch(
        self, memory_id: str, evidence: list[EvidenceItem]
    ) -> None:
        """Write evidence items to memory_evidence table."""
        for ev in evidence:
            self._db.add(MemoryEvidence(
                memory_id=memory_id,
                source_event_id=ev.source_event_id,
                source_type=ev.source_type,
                trust_level=ev.trust_level,
                relation=ev.relation,
                content_span=ev.content_span,
            ))

    def _log_event(
        self, thread_id: str, proposal: MemoryProposal,
        event_type: str, memory_id: str,
    ) -> None:
        """Log a memory lifecycle event."""
        if not thread_id:
            return
        try:
            self._logger.log_event(
                trace_id=proposal.proposal_id,
                thread_id=thread_id,
                event_type=event_type,
                payload={
                    "memory_id": memory_id,
                    "canonical_key": proposal.canonical_key,
                    "memory_type": proposal.memory_type,
                    "operation": event_type,
                },
            )
        except Exception:
            logger.exception("Failed to log event: %s", event_type)

    def _enqueue_projection(
        self,
        record: MemoryRecord,
        event_type: str,
        invalidate_cache: bool = False,
    ) -> None:
        """Enqueue async projection outbox jobs.

        Projection jobs carry memory_id + record_version for stale detection.
        Candidate records skip vector upsert (only active records are searchable).
        """
        import uuid as _uuid

        base_key = f"{record.id}:{record.record_version}"
        is_candidate = record.lifecycle_state == LifecycleState.CANDIDATE.value

        # Core Memory projection refresh: only for keys that feed Core Memory.
        # Enqueued on every write of a core-keyed record so the small, stable
        # projection stays consistent (V2 §五/§九).
        if record.canonical_key and record.canonical_key in self._registry.get_core_memory_keys():
            self._db.add(OutboxJob(
                operation_id=f"core:{base_key}:{_uuid.uuid4().hex[:8]}",
                job_type="core_memory_refresh",
                status="pending",
                payload={"memory_id": record.id, "record_version": record.record_version},
                trace_id=record.id,
                max_retries=3,
            ))

        # Vector upsert: only for active records (candidates not searchable)
        if event_type in ("memory.created", "memory.reinforced", "memory.superseded",
                          "memory.revised", "memory.merged", "memory.wake"):
            if not is_candidate:
                self._db.add(OutboxJob(
                    operation_id=f"vec:{base_key}:{_uuid.uuid4().hex[:8]}",
                    job_type="memory_vector_upsert",
                    status="pending",
                    payload={"memory_id": record.id, "record_version": record.record_version},
                    trace_id=record.id,
                    max_retries=3,
                ))
            # Markdown projection for all records (candidates visible for review)
            self._db.add(OutboxJob(
                operation_id=f"md:{base_key}:{_uuid.uuid4().hex[:8]}",
                job_type="memory_markdown_project",
                status="pending",
                payload={"memory_id": record.id, "record_version": record.record_version},
                trace_id=record.id,
                max_retries=3,
            ))

        # Delete / cache invalidate
        if event_type in ("memory.forgotten", "memory.sleep", "memory.archived",
                          "memory.superseded"):
            self._db.add(OutboxJob(
                operation_id=f"vec-del:{base_key}:{_uuid.uuid4().hex[:8]}",
                job_type="memory_vector_delete",
                status="pending",
                payload={"memory_id": record.id, "record_version": record.record_version},
                trace_id=record.id,
                max_retries=3,
            ))

        if invalidate_cache:
            self._db.add(OutboxJob(
                operation_id=f"cache:{base_key}:{_uuid.uuid4().hex[:8]}",
                job_type="memory_cache_invalidate",
                status="pending",
                payload={"memory_id": record.id, "record_version": record.record_version},
                trace_id=record.id,
                max_retries=3,
            ))

    # ------------------------------------------------------------------
    # Advisory lock helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_lock_id(
        canonical_key: str, scope_type: str, scope_id: str | None
    ) -> int:
        """Compute a deterministic pg_advisory_lock key."""
        raw = f"{canonical_key}|{scope_type}|{scope_id or ''}"
        # Take first 8 bytes of sha256 → signed 64-bit integer
        digest = hashlib.sha256(raw.encode()).digest()[:8]
        return struct.unpack("q", digest)[0]  # type: ignore[no-any-return]

    def _acquire_lock(self, lock_id: int) -> None:
        """Acquire pg_advisory_xact_lock (released at transaction end).

        On SQLite (tests): advisory lock is silently skipped (no-op).
        """
        try:
            self._db.execute(
                __import__("sqlalchemy").text("SELECT pg_advisory_xact_lock(:id)"),
                {"id": lock_id},
            )
        except Exception:
            # SQLite or non-PostgreSQL backend → skip advisory lock
            logger.debug("Advisory lock not available (non-PostgreSQL backend)")

    def _release_lock(self, _lock_id: int) -> None:
        """pg_advisory_xact_lock is auto-released at transaction end.
        This is a no-op kept for API symmetry."""
        pass
