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

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aiive.context.run_context import RunContext
from aiive.db.models import (
    MemoryEvidence,
    MemoryIngestionRun,
    MemoryLineage,
    MemoryProposal as MemoryProposalModel,
    MemoryRecord,
)
from aiive.forget.phase_a_shield import execute_phase_a_shield
from aiive.forget.visibility_service import ForgetVisibilityService
from aiive.memory.conflict_resolver import ConflictResolver, ResolutionResult
from aiive.memory.memory_gate import GateDecision, MemoryGate
from aiive.memory.memory_key_registry import MemoryKeyRegistry, get_memory_key_registry
from aiive.memory.memory_lifecycle_service import MemoryLifecycleService
from aiive.memory.memory_mutation import MemoryMutationExecutor
from aiive.memory.memory_store import MemoryStore
from aiive.memory.memory_types import (
    EvidenceItem,
    LifecycleState,
    LineageOperation,
    MemoryProposal,
    TrustLevel,
    ValidityState,
    WriteOutcome,
)
from aiive.runtime.event_logger import EventLogger

logger = logging.getLogger(__name__)


# ============================================================================
# Write result
# ============================================================================

class WriteResult:
    """Result of a single MemoryWriteService.write() call."""

    outcome: WriteOutcome
    written: bool
    operation: str
    memory_id: str
    state: str
    reason: str
    superseded_ids: list[str]

    def __init__(
        self,
        outcome: WriteOutcome = WriteOutcome.WRITTEN,
        written: bool = False,
        operation: str = "",
        memory_id: str = "",
        state: str = "",
        reason: str = "",
        superseded_ids: list[str] | None = None,
    ) -> None:
        self.outcome = outcome
        self.written = written or outcome == WriteOutcome.WRITTEN
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
# MemoryBatchWriteError
# ============================================================================


class MemoryBatchWriteError(Exception):
    """write_batch 中某个 Proposal 不可恢复失败。"""


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
        # 共享底层 mutation executor + lifecycle service（单向依赖，见 L 节）：
        # promote/execute_maintenance 委托 lifecycle，_enqueue_projection 委托 executor，
        # 全链路复用同一套「锁 + 版本 bump + lineage + 投影」语义。
        self._executor: MemoryMutationExecutor = MemoryMutationExecutor(
            db, registry=self._registry, logger=self._logger
        )
        self._lifecycle: MemoryLifecycleService = MemoryLifecycleService(
            db, executor=self._executor, registry=self._registry
        )

    # ------------------------------------------------------------------
    # write_batch: atomic batch write
    # ------------------------------------------------------------------

    # 合法 no-op outcome，不导致批次回滚
    _VALID_NOOP_OUTCOMES: frozenset[WriteOutcome] = frozenset({
        WriteOutcome.GATE_REJECTED,
        WriteOutcome.IGNORED,
        WriteOutcome.REINFORCE_SKIPPED,
    })

    def write_batch(
        self,
        proposals: list[MemoryProposal],
        ingestion_run: MemoryIngestionRun,
        run_context: RunContext,
    ) -> list[WriteResult]:
        """原子写入一个提取批次。

        前置: ingestion_run 已通过 SELECT FOR UPDATE 锁定，status='running'。
        事务: 所有 Proposal 在同一 Session 中，不自行 commit。
        锁: 在事务开头一次性按序获取所有 advisory lock。
        """
        # Step 0: 收集并排序所有 lock_id
        lock_ids: list[int] = []
        seen: set[int] = set()
        for p in proposals:
            lid = self._compute_lock_id(p.canonical_key, p.scope_type, p.scope_id)
            if lid not in seen:
                seen.add(lid)
                lock_ids.append(lid)
        lock_ids.sort()

        # Step 1: 一次性按序获取所有 advisory lock
        for lid in lock_ids:
            self._acquire_lock(lid)

        # Step 2: 逐 Proposal 写入
        results: list[WriteResult] = []
        for i, p in enumerate(proposals):
            p.ingestion_run_id = ingestion_run.id
            p.proposal_index = i
            p.source_turn_id = run_context.turn_id
            p.source_turn_record_id = run_context.turn_record_id
            p.execution_mode = run_context.execution_mode

            result = self._write_single_in_transaction(p, run_context)
            results.append(result)

            if result.outcome not in self._VALID_NOOP_OUTCOMES and result.outcome != WriteOutcome.WRITTEN:
                raise MemoryBatchWriteError(
                    f"Proposal {i} failed: outcome={result.outcome.value}, reason={result.reason}"
                )

        self._db.flush()
        return results

    def _load_existing_or_wake_sleeping(
        self, proposal: MemoryProposal, run_context: RunContext,
    ) -> Sequence[MemoryRecord]:
        """resolve 前置：加载 active 同键记录；若为空则唤醒 sleeping 同键记录。

        J.4/J.5「wake 闭合」：用户再次确认或显式更新 sleeping 记忆时，write 路径
        必须先唤醒原记录再 resolve，否则 resolver 找不到 active 记录会新建一条，
        造成与旧 sleeping 记忆重复/分叉。唤醒走共享 executor，同事务内改动可见。
        """
        existing = self._store.get_active_by_key_scope_locked(
            proposal.canonical_key, proposal.scope_type, proposal.scope_id,
        )
        if existing:
            return existing
        sleeping = self._store.get_sleeping_by_key_scope_locked(
            proposal.canonical_key, proposal.scope_type, proposal.scope_id,
        )
        if sleeping:
            # 唤醒最近更新的一条 sleeping 记录，再重新加载 active 视野
            self._lifecycle.wake(
                sleeping[0].id,
                trace_id=run_context.trace_id or "",
                thread_id=run_context.thread_id,
            )
            existing = self._store.get_active_by_key_scope_locked(
                proposal.canonical_key, proposal.scope_type, proposal.scope_id,
            )
        return existing

    def _write_single_in_transaction(
        self, proposal: MemoryProposal, run_context: RunContext,
    ) -> WriteResult:
        """单个 Proposal 的 Gate → Resolve → Execute → Persist 流程。

        不再获取/释放 advisory lock（由 write_batch 统一管理）。
        """
        gate_decision = self._gate.decide(proposal)
        if gate_decision.decision == "reject":
            self._persist_proposal(proposal, gate_decision, final_op="reject")
            return WriteResult(outcome=WriteOutcome.GATE_REJECTED, reason=gate_decision.reason)

        # 1.5. Phase 6A: 防重抽检查（tombstone.block_reingestion）
        # write_batch 异步抽取路径同样必须拦截，避免被忘事实从被屏蔽旧源事件重建。
        if proposal.evidence:
            blocked_events: set[str] = set()
            for ev in proposal.evidence:
                if ev.source_event_id and ForgetVisibilityService.is_tombstone_blocked_reingestion(
                    self._db, source_event_id=ev.source_event_id,
                ):
                    blocked_events.add(ev.source_event_id)
            all_events = {ev.source_event_id for ev in proposal.evidence if ev.source_event_id}
            if all_events and all_events == blocked_events:
                return WriteResult(
                    outcome=WriteOutcome.GATE_REJECTED,
                    reason="Reingestion blocked: all source events are tombstone-blocked",
                )

        existing_active = self._load_existing_or_wake_sleeping(proposal, run_context)
        resolution = self._resolver.resolve(proposal, existing_active)
        tid = run_context.thread_id

        if resolution.operation == "reinforce":
            return self._execute_reinforce_in_transaction(
                proposal, resolution.existing_record or existing_active[0],
                gate_decision, tid,
            )
        elif resolution.operation in ("supersede", "revise"):
            return self._execute_supersede_or_revise(
                proposal, resolution, gate_decision, tid,
            )
        elif resolution.operation == "promote":
            return self._execute_promote_candidate(proposal, resolution, gate_decision, tid)
        elif resolution.operation == "merge":
            return self._execute_merge(proposal, resolution, gate_decision, tid)
        elif resolution.operation == "ignore":
            self._persist_proposal(proposal, gate_decision, final_op="ignore")
            return WriteResult(outcome=WriteOutcome.IGNORED, reason=resolution.reason)
        else:
            target_state = (
                LifecycleState.CANDIDATE.value
                if gate_decision.decision == "candidate"
                else LifecycleState.ACTIVE.value
            )
            record = self._store.create_record(
                proposal=proposal, lifecycle_state=target_state,
                validity_state=ValidityState.VALID.value,
            )
            self._write_evidence_batch(record.id, proposal.evidence)
            self._persist_proposal(proposal, gate_decision, final_op="create", final_memory_id=record.id)
            self._log_event(tid, proposal, "memory.created", record.id)
            self._enqueue_projection(record, "memory.created")
            return WriteResult(outcome=WriteOutcome.WRITTEN, operation="create",
                               memory_id=record.id, state=target_state)

    def _execute_reinforce_in_transaction(
        self,
        proposal: MemoryProposal,
        existing: MemoryRecord,
        gate_decision: GateDecision,
        thread_id: str,
    ) -> WriteResult:
        """Reinforce variant for batch writes。"""
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
                self._persist_proposal(proposal, gate_decision, final_op="ignore", final_memory_id=existing.id)
                return WriteResult(
                    outcome=WriteOutcome.REINFORCE_SKIPPED,
                    reason="All source_events already counted",
                )
        else:
            new_count = 1

        trust_mult = 0.1 if proposal.trust_level == TrustLevel.TRUSTED.value else 0.05
        existing.confidence = min(1.0, (existing.confidence or 0.5) + trust_mult * new_count)
        existing.reinforce_count = (existing.reinforce_count or 0) + new_count
        existing.last_reinforced_at = datetime.now(timezone.utc)
        existing.observed_at = datetime.now(timezone.utc)
        existing.record_version += 1
        existing.updated_at = datetime.now(timezone.utc)

        self._write_evidence_batch(existing.id, proposal.evidence)
        self._persist_proposal(proposal, gate_decision, final_op="reinforce", final_memory_id=existing.id)
        self._log_event(thread_id, proposal, "memory.reinforced", existing.id)
        self._enqueue_projection(existing, "memory.reinforced", invalidate_cache=True)
        return WriteResult(outcome=WriteOutcome.WRITTEN, written=True, operation="reinforce",
                           memory_id=existing.id, state=LifecycleState.ACTIVE.value)

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
                outcome=WriteOutcome.GATE_REJECTED,
                reason=f"Gate rejected: {gate_decision.reason}",
            )

        # 1.5. Phase 6A: 防重抽检查（tombstone.block_reingestion）
        if proposal.evidence:
            blocked_events: set[str] = set()
            for ev in proposal.evidence:
                if ev.source_event_id:
                    if ForgetVisibilityService.is_tombstone_blocked_reingestion(
                        self._db, source_event_id=ev.source_event_id,
                    ):
                        blocked_events.add(ev.source_event_id)
            # 所有 evidence 都来自 blocked source → 拒绝
            all_events = {ev.source_event_id for ev in proposal.evidence if ev.source_event_id}
            if all_events and all_events == blocked_events:
                return WriteResult(
                    outcome=WriteOutcome.GATE_REJECTED,
                    reason="Reingestion blocked: all source events are tombstone-blocked",
                )

        # 2. Acquire advisory lock (hash of canonical_key + scope)
        lock_id: int = self._compute_lock_id(
            proposal.canonical_key, proposal.scope_type, proposal.scope_id
        )
        self._acquire_lock(lock_id)

        try:
            # 3. SELECT FOR UPDATE existing active records（无 active 则先唤醒 sleeping 同键记录）
            existing_active: Sequence[MemoryRecord] = self._load_existing_or_wake_sleeping(
                proposal, run_context
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
        """Execute a maintenance proposal (sleep/archive/wake) on an existing record.

        委托到 `MemoryLifecycleService`：走共享 executor（版本 bump + lineage +
        投影），消除此前 `self._store.update_lifecycle` 的无版本校验旁路（L 节）。
        """
        tid: str = run_context.thread_id
        trace: str = run_context.trace_id
        target_memory_id: str = proposal.source_event_ids[0] if proposal.source_event_ids else ""

        if not target_memory_id:
            return WriteResult(written=False, reason="No target memory_id in maintenance proposal")

        record: MemoryRecord | None = self._store.get_by_id(target_memory_id)
        if record is None:
            return WriteResult(written=False, reason=f"Memory {target_memory_id} not found")

        op: str = proposal.proposed_operation

        if op == "sleep":
            ok, reason = self._lifecycle.sleep(record.id, trace_id=trace, thread_id=tid)
            return WriteResult(written=ok, operation="sleep", memory_id=record.id,
                               reason="" if ok else reason)

        if op == "archive":
            ok, reason = self._lifecycle.archive(record.id, trace_id=trace, thread_id=tid)
            return WriteResult(written=ok, operation="archive", memory_id=record.id,
                               reason="" if ok else reason)

        if op == "wake":
            ok = self._lifecycle.wake(record.id, trace_id=trace, thread_id=tid)
            return WriteResult(written=ok, operation="wake", memory_id=record.id,
                               reason="" if ok else "Cannot wake: record is not sleeping")

        return WriteResult(written=False, reason=f"Unknown maintenance operation: {op}")

    # ------------------------------------------------------------------
    # Forget (Saga — covers all layers)
    # ------------------------------------------------------------------

    def forget(
        self,
        memory_id: str = "",
        reason: str = "",
        run_context: RunContext | None = None,
        *,
        # Phase 6A 扩展参数
        mode: str = "everywhere",
        memory_ids: list[str] | None = None,
        turn_ids: list[str] | None = None,
        event_ids: list[str] | None = None,
        thread_id: str = "",
        canonical_key: str = "",
        scope_type: str = "",
        scope_id: str = "",
        all_user_data: bool = False,
        operation_key: str = "",
    ) -> WriteResult:
        """执行 Phase 6A Forget Saga — Phase A 立即屏蔽。

        单事务内完成：Operation → SelectorManifest → Shield → Tombstone →
        MemoryRecord.forgotten → CoreMemoryBlock 删除 → Retrieval tombstone →
        Cascade OutboxJob + ForgetStageRun。

        向后兼容旧 API（仅传 memory_id → 自动转 memory_only + memory_ids=[memory_id]）。
        """
        # 向后兼容旧 API
        if memory_id and not memory_ids:
            memory_ids = [memory_id]
            if mode == "everywhere":
                mode = "memory_only"

        if not memory_ids and not turn_ids and not event_ids and not thread_id \
                and not canonical_key and not all_user_data \
                and not (scope_type and scope_id):
            return WriteResult(written=False, reason="No targets specified for forget")

        try:
            result = execute_phase_a_shield(
                session=self._db,
                mode=mode,
                memory_ids=memory_ids,
                turn_ids=turn_ids,
                event_ids=event_ids,
                thread_id=thread_id if thread_id else None,
                canonical_key=canonical_key if canonical_key else None,
                scope_type=scope_type if scope_type else None,
                scope_id=scope_id if scope_id else None,
                all_user_data=all_user_data,
                reason=reason,
                requested_by=run_context.trace_id if run_context else "",
                operation_key=operation_key if operation_key else None,
            )

            # 记录 tombstone event（向后兼容）
            if run_context and run_context.thread_id:
                try:
                    self._executor.log_event(
                        trace_id=run_context.trace_id,
                        event_type="memory.forgotten",
                        memory_id=memory_ids[0] if memory_ids else "",
                        payload={
                            "operation_key": result["operation_key"],
                            "mode": mode,
                            "target_count": result["target_count"],
                            "reason": reason,
                        },
                        thread_id=run_context.thread_id,
                    )
                except Exception:
                    pass  # 隔离写入

            return WriteResult(
                written=True,
                operation="forget",
                memory_id=memory_ids[0] if memory_ids else "",
                reason=f"Phase A shielded: {result['operation_key']}",
            )
        except Exception as exc:
            logger.exception("Phase A shield failed")
            return WriteResult(written=False, reason=f"Forget failed: {exc}")

    # ------------------------------------------------------------------
    # Promotion: candidate → active
    # ------------------------------------------------------------------

    def promote(self, memory_id: str, run_context: RunContext) -> WriteResult:
        """Promote a candidate record to active (with lineage).

        委托到 `MemoryLifecycleService.promote`：走共享 executor（版本 bump +
        lineage + 投影），不再直接改写 lifecycle（L 节）。
        """
        ok, reason = self._lifecycle.promote(
            memory_id,
            trace_id=run_context.trace_id,
            thread_id=run_context.thread_id,
            reason="Candidate promoted to active",
        )
        if not ok:
            return WriteResult(written=False, reason=reason)
        return WriteResult(written=True, operation="promote", memory_id=memory_id)

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
        # 新记录作为 active 记忆写入向量库（upsert，不删除）
        self._enqueue_projection(new_record, "memory.created", invalidate_cache=True)
        # 旧记录已失效，从向量库移除其旧向量（delete only）
        self._enqueue_projection(old, "memory.superseded", invalidate_cache=True)

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
            sensitivity=proposal.sensitivity,
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
            # Phase 2: execution mode + retention + durability
            execution_mode=proposal.execution_mode,
            retention_policy=proposal.retention_policy,
            valid_to=proposal.valid_to,
            durable=proposal.durable,
            source_turn_record_id=proposal.source_turn_record_id or None,
        )
        # 防御层：极端并发/重试下唯一约束仍可能冲突，用保存点隔离单次插入。
        # 冲突时回滚到保存点、确认后跳过，绝不污染整条事务（PendingRollbackError）。
        try:
            with self._db.begin_nested():
                self._db.add(mp)
                self._db.flush()
        except IntegrityError:
            self._db.expunge(mp)
            if ikey:
                recheck = self._db.query(MemoryProposalModel).filter(
                    MemoryProposalModel.idempotency_key == ikey
                ).first()
                if recheck is not None:
                    logger.warning(
                        "幂等键冲突，跳过重复 proposal: %s", ikey
                    )
                    return
            logger.exception("Proposal 幂等键冲突但记录缺失，需排查: %s", ikey)
            raise

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
        """记录一条记忆生命周期事件。

        委托共享 `MemoryMutationExecutor.log_event`：事件写入在 SAVEPOINT 内单独
        flush 并隔离，失败仅丢弃事件本身，不污染外层维护事务。
        """
        if not thread_id:
            return
        self._executor.log_event(
            trace_id=proposal.proposal_id,
            event_type=event_type,
            memory_id=memory_id,
            payload={
                "memory_id": memory_id,
                "canonical_key": proposal.canonical_key,
                "memory_type": proposal.memory_type,
                "operation": event_type,
            },
            thread_id=thread_id,
        )

    def _enqueue_projection(
        self,
        record: MemoryRecord,
        event_type: str,
        invalidate_cache: bool = False,
    ) -> None:
        """Enqueue async projection outbox jobs.

        委托到共享的 `MemoryMutationExecutor.enqueue_projection`（唯一投影入队入口），
        与生命周期维护路径复用同一套投影语义，避免出现两份逻辑（L 节）。
        """
        self._executor.enqueue_projection(record, event_type, invalidate_cache)

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
