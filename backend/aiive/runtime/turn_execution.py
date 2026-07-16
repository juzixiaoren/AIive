"""
TurnExecutionService：统一的 Turn 生命周期管理 (Phase 1).

Phase 1: 使用 ContextAssembler 保证有界上下文。
"""
from __future__ import annotations

import hashlib
import json as _json
import logging
import threading
import uuid as _uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy import func

from aiive.core.llm_client import LLMClient, default_llm_client
from aiive.db.base import SessionLocal
from aiive.db.models import (
    ContextSnapshot,
    Epoch,
    Event,
    LLMCall,
    OutboxJob,
    Segment,
    Thread,
    TurnRecord,
)
from aiive.memory.extraction_policy import MemorySignalAction
from aiive.runtime.agent_graph import AgentGraph, AgentGraphResult, _flatten_tool_result  # pyright: ignore[reportPrivateUsage]
from aiive.runtime.context_assembler import (
    AssembledContext,
    ContextAssembler,
    ContextBudgetExceededError,
)
from aiive.runtime.context_budget import ContextBudget
from aiive.runtime.epoch_manager import EpochManager
from aiive.runtime.thread_bootstrap import ThreadBootstrapService
from aiive.runtime.token_counter import LiteLLMTokenCounter
from aiive.runtime.token_models import ModelProfile, TokenSafetyConfig
from aiive.runtime.tool_normalizer import ToolResultNormalizer
from aiive.runtime.working_state import WorkingStateService

LEASE_DURATION = timedelta(seconds=300)
HEARTBEAT_INTERVAL = 60


def _compute_request_fingerprint(message: str, thread_id: str | None) -> str:
    canonical = _json.dumps({
        "message": message,
        "thread_id": thread_id or "",
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _next_turn_sequence(db: Session, thread_id: str) -> int:
    """在 Thread 行锁保护下计算下一个递增 turn_sequence。

    返回 (当前最大 turn_sequence 或 0) + 1，作为新 Turn 的不可变序号。
    """
    max_seq = db.query(func.max(TurnRecord.turn_sequence)).filter(
        TurnRecord.thread_id == thread_id,
    ).scalar()
    return (max_seq or 0) + 1


class TurnConflictError(Exception):
    """Turn idempotency conflict."""


class FencingViolationError(Exception):
    """Turn fencing/lease violation."""


# ============================================================================
# TurnHeartbeat
# ============================================================================


class TurnHeartbeat:
    def __init__(self, turn_record_id: str, execution_id: str):
        self._turn_record_id: str = turn_record_id
        self._execution_id: str = execution_id
        self._stop_event: threading.Event = threading.Event()
        self._lease_lost: threading.Event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started: bool = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="turn-heartbeat")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)

    @property
    def lease_lost(self) -> bool:
        return self._lease_lost.is_set()

    def _loop(self) -> None:
        while not self._stop_event.wait(HEARTBEAT_INTERVAL):
            db: Session | None = None
            try:
                db = SessionLocal()
                now = datetime.now(timezone.utc)
                affected = db.query(TurnRecord).filter(
                    TurnRecord.id == self._turn_record_id,
                    TurnRecord.execution_id == self._execution_id,
                    TurnRecord.status == "running",
                ).update({
                    TurnRecord.lease_expires_at: now + LEASE_DURATION,
                    TurnRecord.last_heartbeat_at: now,
                    TurnRecord.updated_at: now,
                }, synchronize_session=False)
                db.commit()
                if affected == 0:
                    self._lease_lost.set()
            except Exception:
                logger.exception("Heartbeat error")
            finally:
                if db is not None:
                    db.close()


# ============================================================================
# ContextBundle (pure data)
# ============================================================================


@dataclass
class ContextBundle:
    thread_id: str
    system_content: str
    recall_messages: list[dict[str, Any]]
    core_blocks: list[dict[str, Any]]
    recall_pack_items: list[dict[str, Any]]
    context_snapshot_items: list[dict[str, Any]]
    context_snapshot_meta: dict[str, Any]
    history_events: list[dict[str, Any]]
    recall_run_id: str = ""
    # Phase 1 additions
    assembled_ctx: AssembledContext | None = None


# ============================================================================
# TurnExecutionService
# ============================================================================


class TurnExecutionService:
    """Unified Turn lifecycle (Phase 1: bounded context)."""

    def __init__(self, llm_client: LLMClient | None = None, source: str = "user_chat"):
        self._llm_client: LLMClient = llm_client or default_llm_client()
        self._source: str = source
        self._profile: ModelProfile = ModelProfile.from_config("deepseek", self._llm_client.default_model)
        self._safety: TokenSafetyConfig = TokenSafetyConfig()
        self._token_counter: LiteLLMTokenCounter = LiteLLMTokenCounter(self._safety, self._profile)
        self._budget: ContextBudget = ContextBudget.from_env()
        self._budget.validate()
        self._normalizer: ToolResultNormalizer = ToolResultNormalizer(self._token_counter, self._profile.full_name)
        self._epoch_mgr: EpochManager = EpochManager()
        self._ws_service: WorkingStateService = WorkingStateService()

    # =====================================================================
    # Public API
    # =====================================================================

    def execute_turn(
        self, message: str, thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        return self._execute_turn_impl(message, thread_id, turn_id)

    async def execute_turn_stream(
        self, message: str, thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """流式执行 Turn：复用同一 ContextAssembler 有界上下文，产出 SSE 事件。"""
        try:
            result = self._execute_turn_impl(message, thread_id, turn_id)
        except ContextBudgetExceededError as e:
            yield {"type": "error", "error": "context_budget_exceeded",
                  "safe_tokens": e.safe_tokens, "context_window": e.context_window}
            return
        except TurnConflictError as e:
            yield {"type": "error", "error": str(e)}
            return
        except Exception:
            logger.exception("流式 Turn 执行失败")
            yield {"type": "error", "error": "internal_error"}
            return

        yield {"type": "done", **result}

    # =====================================================================
    # Phase 1: Resolve, preempt, attribute epoch/segment
    # =====================================================================

    def _resolve_and_preempt(
        self, message: str, thread_id: str | None, turn_id: str | None,
    ) -> tuple[TurnRecord, str]:
        turn_id = turn_id or str(_uuid.uuid4())
        fingerprint = _compute_request_fingerprint(message, thread_id)
        committed_tid = ThreadBootstrapService.ensure_committed_thread(thread_id)
        execution_id = str(_uuid.uuid4())

        # ── Short transaction: Thread row lock + epoch/segment attribution ──
        db = SessionLocal()
        turn_record_id = ""
        try:
            # Ensure Thread row exists (lazy creation if needed)
            existing_thread = db.get(Thread, committed_tid)
            if existing_thread is None:
                existing_thread = Thread(id=committed_tid)
                db.add(existing_thread)
                db.flush()
            # Lock Thread row for epoch/segment serialization
            db.query(Thread).with_for_update().filter(Thread.id == committed_tid).one()

            existing = db.query(TurnRecord).filter(
                TurnRecord.thread_id == committed_tid,
                TurnRecord.turn_id == turn_id,
            ).first()

            if existing is None:
                # Attribute epoch/segment（在 Thread 行锁保护下计算递增 turn_sequence）
                try:
                    next_seq = _next_turn_sequence(db, committed_tid)
                    epoch, segment = self._epoch_mgr.ensure_epoch_and_segment(
                        db, committed_tid, next_seq,
                    )
                except IntegrityError:
                    db.rollback()
                    db.close()
                    # Re-read: another concurrent request created epoch/segment
                    db2 = SessionLocal()
                    try:
                        db2.query(Thread).with_for_update().filter(Thread.id == committed_tid).one()
                        next_seq = _next_turn_sequence(db2, committed_tid)
                        epoch, segment = self._epoch_mgr.ensure_epoch_and_segment(db2, committed_tid, next_seq)
                    finally:
                        db2.close()
                    db = SessionLocal()
                    db.query(Thread).with_for_update().filter(Thread.id == committed_tid).one()
                    epoch = db.query(Epoch).filter(Epoch.thread_id == committed_tid, Epoch.status == "active").first()
                    if epoch is not None:
                        segment = db.query(Segment).filter(Segment.epoch_id == epoch.id, Segment.status == "open").first()
                    else:
                        segment = None

                turn = TurnRecord(
                    thread_id=committed_tid,
                    turn_id=turn_id,
                    turn_sequence=next_seq,
                    status="not_started",
                    attempt_no=1,
                    request_fingerprint=fingerprint,
                    epoch_id=epoch.id if epoch else None,
                    segment_id=segment.id if segment else None,
                )
                event_id = str(_uuid.uuid4())
                event = Event(
                    id=event_id, thread_id=committed_tid, turn_id=turn_id,
                    turn_event_index=0, trace_id="", event_type="user_message",
                    payload={"content": message},
                )
                db.add(turn)
                db.add(event)
                db.flush()
                turn.request_event_id = event.id
                db.commit()
                db.refresh(turn)
                turn_record_id = turn.id
            else:
                # Existing Turn logic unchanged
                if existing.status == "completed":
                    if existing.request_fingerprint != fingerprint:
                        raise TurnConflictError("idempotency_key_mismatch")
                    cached = existing.response_payload or {}
                    cached["_turn_cached"] = True
                    raise TurnConflictError("turn_cached")
                if existing.status == "running":
                    if existing.lease_expires_at and existing.lease_expires_at > datetime.now(timezone.utc):
                        raise TurnConflictError("turn_in_progress")
                    self._mark_interrupted(db, existing.id, existing.execution_id or "")
                    db.commit()
                    raise TurnConflictError("turn_interrupted")
                if existing.status == "interrupted_unknown":
                    raise TurnConflictError("turn_interrupted")
                if existing.status == "not_started":
                    if existing.request_fingerprint != fingerprint:
                        raise TurnConflictError("idempotency_key_mismatch")
                    existing.attempt_no = (existing.attempt_no or 1) + 1
                    db.commit()
                    turn_record_id = existing.id
        finally:
            db.close()

        # ── Atomic preemption ──
        db2 = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            affected = db2.query(TurnRecord).filter(
                TurnRecord.id == turn_record_id,
                TurnRecord.status == "not_started",
            ).update({
                TurnRecord.status: "running",
                TurnRecord.execution_id: execution_id,
                TurnRecord.lease_expires_at: now + LEASE_DURATION,
                TurnRecord.updated_at: now,
            }, synchronize_session=False)
            db2.commit()
            if affected != 1:
                turn = db2.get(TurnRecord, turn_record_id)
                if turn and turn.status == "completed":
                    if turn.request_fingerprint != fingerprint:
                        raise TurnConflictError("idempotency_key_mismatch")
                    raise TurnConflictError("turn_cached")
                raise TurnConflictError("preemption_failed")
        finally:
            db2.close()

        # Reload
        db3 = SessionLocal()
        try:
            turn = db3.get(TurnRecord, turn_record_id)
            if turn is None or turn.status != "running":
                raise TurnConflictError("preemption_failed")
            result = TurnRecord(
                id=turn.id, thread_id=turn.thread_id, turn_id=turn.turn_id,
                turn_sequence=turn.turn_sequence, status=turn.status,
                execution_id=turn.execution_id, request_event_id=turn.request_event_id,
                request_fingerprint=turn.request_fingerprint,
                epoch_id=turn.epoch_id, segment_id=turn.segment_id,
            )
            return result, execution_id
        finally:
            db3.close()

    def _mark_interrupted(self, db: Session, turn_record_id: str, execution_id: str) -> None:
        db.query(TurnRecord).filter(
            TurnRecord.id == turn_record_id,
            TurnRecord.execution_id == execution_id,
            TurnRecord.status == "running",
        ).update({
            TurnRecord.status: "interrupted_unknown",
            TurnRecord.lease_expires_at: None,
            TurnRecord.updated_at: datetime.now(timezone.utc),
        }, synchronize_session=False)

    # =====================================================================
    # Phase 5: Finalize (with snapshot rotation)
    # =====================================================================

    def _finalize_turn(
        self, turn: TurnRecord, execution_id: str,
        ag_result: AgentGraphResult, heartbeat: TurnHeartbeat,
        assembled_ctx: AssembledContext | None = None,
    ) -> dict[str, Any]:
        if heartbeat.lease_lost:
            raise FencingViolationError("lease lost")

        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            response_dict = {
                "reply": ag_result.reply, "thread_id": turn.thread_id,
                "trace_id": ag_result.trace_id, "action_cards": ag_result.action_cards,
                "intent_type": "plain_chat",
                "tool_calls": [{"name": r.name, "params": r.params, "status": r.status} for r in ag_result.tool_records],
                "tool_results": [
                    {"name": r.name, "params": r.params, "result": _flatten_tool_result(r.result), "status": r.status}
                    for r in ag_result.tool_records if r.status in ("completed", "failed")
                ],
                "parse_errors": [],
            }

            affected = db.query(TurnRecord).filter(
                TurnRecord.id == turn.id,
                TurnRecord.execution_id == execution_id,
                TurnRecord.status == "running",
            ).update({
                TurnRecord.status: "completed",
                TurnRecord.response_payload: response_dict,
                TurnRecord.lease_expires_at: None,
                TurnRecord.last_heartbeat_at: None,
                TurnRecord.updated_at: now,
            }, synchronize_session=False)
            db.flush()
            if affected != 1:
                db.rollback()
                raise FencingViolationError("fencing violation")

            # Events (unchanged from 0.5A)
            idx = 1
            for r in ag_result.tool_records:
                db.add(Event(id=str(_uuid.uuid4()), trace_id=ag_result.trace_id, thread_id=turn.thread_id,
                             event_type="tool_call", turn_id=turn.turn_id, turn_event_index=idx,
                             payload={"name": r.name, "params": r.params, "tool_call_id": r.tool_call_id, "batch_index": r.batch_index}))
                idx += 1
                db.add(Event(id=str(_uuid.uuid4()), trace_id=ag_result.trace_id, thread_id=turn.thread_id,
                             event_type="tool_result", turn_id=turn.turn_id, turn_event_index=idx,
                             payload={"name": r.name, "result": r.result, "status": r.status, "tool_call_id": r.tool_call_id, "batch_index": r.batch_index}))
                idx += 1
            assistant_event_id = str(_uuid.uuid4())
            db.add(Event(id=assistant_event_id, trace_id=ag_result.trace_id, thread_id=turn.thread_id,
                         event_type="llm_response", turn_id=turn.turn_id, turn_event_index=idx,
                         payload={"content": ag_result.reply, "action_cards": ag_result.action_cards}))
            idx += 1
            db.add(Event(id=str(_uuid.uuid4()), trace_id=ag_result.trace_id, thread_id=turn.thread_id,
                         event_type="chat_ended", turn_id=turn.turn_id, turn_event_index=idx,
                         payload={"tool_calls": len(ag_result.tool_records), "tool_succeeded": sum(1 for r in ag_result.tool_records if r.status == "completed"), "tool_failed": sum(1 for r in ag_result.tool_records if r.status == "failed"), "parse_errors": 0}))

            # ContextSnapshot with Phase 1 rotation
            self._rotate_snapshot(db, turn, ag_result, assembled_ctx)

            # Phase 1: write token estimation record for observability (§7.7)
            if assembled_ctx is not None:
                tc = assembled_ctx.total_token_count
                db.add(LLMCall(
                    id=str(_uuid.uuid4()),
                    trace_id=ag_result.trace_id,
                    thread_id=turn.thread_id,
                    model=self._profile.full_name,
                    latency_ms=0,
                    input_preview=(ag_result.user_message or "")[:200],
                    output_preview=ag_result.reply[:200] if ag_result.reply else None,
                    estimated_prompt_tokens=tc.estimated_tokens,
                    safe_prompt_tokens=tc.safe_tokens,
                    actual_prompt_tokens=None,
                    actual_completion_tokens=None,
                    token_source=tc.source,
                ))

            # Outbox — Phase 2: 写入真实 Event.id 到 source_event_ids
            if ag_result.memory_signal.action == MemorySignalAction.EXTRACT_ASYNC.value:
                db.add(OutboxJob(
                    operation_id=f"memory_extraction:{turn.id}:{_uuid.uuid4().hex[:8]}",
                    job_type="memory_extraction", status="pending",
                    payload={
                        "user_message": getattr(ag_result, 'user_message', ''),
                        "reply": ag_result.reply,
                        "thread_id": turn.thread_id,
                        "source_turn_record_id": turn.id,
                        "source_turn_id": turn.turn_id,
                        "source_event_ids": [
                            turn.request_event_id,    # 用户消息真实 Event.id
                            assistant_event_id,        # Assistant 回复真实 Event.id
                        ],
                    },
                    trace_id=ag_result.trace_id, max_retries=3,
                ))

            db.commit()
            return response_dict
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _rotate_snapshot(
        self, db: Session, turn: TurnRecord,
        ag_result: AgentGraphResult, assembled_ctx: AssembledContext | None,
    ) -> None:
        """Atomic snapshot rotation by turn_sequence."""
        db.query(Thread).with_for_update().filter(Thread.id == turn.thread_id).one()

        current = db.query(ContextSnapshot).filter(
            ContextSnapshot.thread_id == turn.thread_id,
            ContextSnapshot.retention == "current",
        ).first()

        total_tokens = assembled_ctx.total_token_count.safe_tokens if assembled_ctx else 0
        seq = turn.turn_sequence or 0

        new_snap = ContextSnapshot(
            trace_id=ag_result.trace_id,
            thread_id=turn.thread_id,
            stable_prefix_hash=ag_result.context_snapshot_meta.get("stable_prefix_hash", ""),
            context_items=ag_result.context_snapshot_meta.get("context_items", []),
            meta={"total_tool_calls": len(ag_result.tool_records)},
            epoch_id=turn.epoch_id,
            segment_id=turn.segment_id,
            turn_sequence=seq,
            token_total=total_tokens,
        )

        if current is None or seq > (current.turn_sequence or 0):
            db.query(ContextSnapshot).filter(
                ContextSnapshot.thread_id == turn.thread_id,
                ContextSnapshot.retention == "previous",
            ).update({"retention": "temporary"})
            if current:
                current.retention = "previous"
            new_snap.retention = "current"
        else:
            new_snap.retention = "temporary"

        db.add(new_snap)

        # 乱序完成且无审计价值的 temporary 快照直接删除，不持久保留（§14.1）
        db.query(ContextSnapshot).filter(
            ContextSnapshot.thread_id == turn.thread_id,
            ContextSnapshot.retention == "temporary",
        ).delete()

        # 审计快照上限 5（保留最新），保证 current≤1 / previous≤1 / audit≤5 / 总数≤7
        audit_excess = (
            db.query(ContextSnapshot)
            .filter(
                ContextSnapshot.thread_id == turn.thread_id,
                ContextSnapshot.retention == "audit",
            )
            .order_by(ContextSnapshot.turn_sequence.desc())
            .offset(5)
            .all()
        )
        for snap in audit_excess:
            db.delete(snap)

    # =====================================================================
    # Shared lifecycle
    # =====================================================================

    def _execute_turn_impl(
        self, message: str, thread_id: str | None, turn_id: str | None,
    ):
        try:
            turn, execution_id = self._resolve_and_preempt(message, thread_id, turn_id)
        except TurnConflictError as e:
            err_msg = str(e)
            if err_msg == "turn_cached":
                db = SessionLocal()
                try:
                    existing = db.query(TurnRecord).filter(
                        TurnRecord.thread_id == ThreadBootstrapService.ensure_committed_thread(thread_id),
                        TurnRecord.turn_id == (turn_id or ""),
                    ).first()
                    if existing and existing.response_payload:
                        return existing.response_payload
                finally:
                    db.close()
                return {"reply": "", "error": "turn_cached_no_payload"}
            if err_msg == "turn_in_progress":
                return {"reply": "", "error": "turn_in_progress", "_status": 202}
            return {"reply": "", "error": err_msg, "_status": 409}

        heartbeat = TurnHeartbeat(turn.id, execution_id)
        heartbeat.start()

        # Crash recovery: clean orphaned running tools
        recovery_db = SessionLocal()
        try:
            self._ws_service.recover_orphaned_tools(recovery_db, turn.thread_id)
            recovery_db.commit()
        finally:
            recovery_db.close()

        try:
            # Phase 2: Context assembly (via ContextAssembler)
            ctx_bundle = self._load_context(message, turn)

            # Phase 3: AgentGraph execution
            graph_db = SessionLocal()
            try:
                graph = AgentGraph(self._llm_client, graph_db)
                ag_result = graph._execute_graph(  # pyright: ignore[reportPrivateUsage]
                    message=message, thread_id=turn.thread_id,
                    turn_id=turn.turn_id, ctx_bundle=ctx_bundle,
                    execution_id=execution_id,
                )
            finally:
                graph_db.close()

            # Phase 4: Memory extraction (outside transaction)
            if ag_result.memory_signal.action == MemorySignalAction.EXTRACT_SYNC.value:
                try:
                    from aiive.memory.memory_extractor import UnifiedMemoryExtractor
                    from aiive.memory.memory_write_service import MemoryWriteService
                    from aiive.context.run_context import RunContext
                    extractor = UnifiedMemoryExtractor(self._llm_client)
                    sync_proposals = extractor.extract(
                        user_message=message, reply=ag_result.reply,
                        trace_id=ag_result.trace_id, thread_id=turn.thread_id,
                    )
                    if sync_proposals:
                        writer_db = SessionLocal()
                        try:
                            writer = MemoryWriteService(writer_db)
                            for proposal in sync_proposals:
                                proposal.source_turn_id = turn.turn_id
                                run_ctx = RunContext(thread_id=turn.thread_id, trace_id=ag_result.trace_id, source="sync_extract", turn_id=turn.turn_id)
                                writer.write(proposal, run_context=run_ctx)
                            writer_db.commit()
                        finally:
                            writer_db.close()
                except Exception:
                    logger.exception("Sync memory extraction failed (non-fatal)")

            # Phase 5: Finalize
            assembled_ctx = ctx_bundle.assembled_ctx if ctx_bundle else None
            response = self._finalize_turn(turn, execution_id, ag_result, heartbeat, assembled_ctx)
            return response

        except ContextBudgetExceededError as e:
            logger.error("Context budget exceeded: %s", e)
            db = SessionLocal()
            try:
                self._mark_interrupted(db, turn.id, execution_id)
                db.commit()
            finally:
                db.close()
            return {"reply": "", "error": "context_budget_exceeded", "safe_tokens": e.safe_tokens, "context_window": e.context_window, "_status": 413}
        except Exception:
            db = SessionLocal()
            try:
                self._mark_interrupted(db, turn.id, execution_id)
                db.commit()
            except Exception:
                logger.exception("mark_interrupted failed")
            finally:
                db.close()
            raise
        finally:
            heartbeat.stop()

    # =====================================================================
    # Phase 2: Context loading via ContextAssembler
    # =====================================================================

    def _load_context(self, message: str, turn: TurnRecord) -> ContextBundle:
        db = SessionLocal()
        try:
            thread = db.get(Thread, turn.thread_id)
            if thread is None:
                thread = Thread(id=turn.thread_id)
                db.add(thread)
                db.flush()

            assembler = ContextAssembler(
                token_counter=self._token_counter,
                budget=self._budget,
                profile=self._profile,
                normalizer=self._normalizer,
            )

            assembled = assembler.assemble(
                db=db,
                message=message,
                thread=thread,
                _source=self._source,
                upper_bound_sequence=(turn.turn_sequence or 1) - 1,
            )

            ag_ctx = assembled.agent_ctx
            return ContextBundle(
                thread_id=thread.id,
                system_content=ag_ctx.get("system_content", ""),
                recall_messages=ag_ctx.get("recall_messages", []),
                core_blocks=[],  # Not needed by _execute_graph when assembled_ctx is available
                recall_pack_items=[],
                context_snapshot_items=[],
                context_snapshot_meta={},
                history_events=[],  # AssembledContext.messages replaces this
                assembled_ctx=assembled,
            )
        finally:
            db.close()
