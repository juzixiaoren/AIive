"""Phase 0.5B: Outbox handlers — unified (ClaimedJob) -> HandlerResult interface."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiive.worker.handler_registry import HandlerRegistry

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from aiive.config import settings
from aiive.context.run_context import RunContext
from aiive.core.llm_client import LLMClient
from aiive.db.base import SessionLocal
from aiive.db.models import MemoryIngestionRun, OutboxJob, TurnRecord
from aiive.memory.extraction_policy import MemoryExtractionPolicy
from aiive.memory.memory_extractor import UnifiedMemoryExtractor
from aiive.memory.memory_types import MemoryProposal
from aiive.memory.memory_write_service import MemoryBatchWriteError, MemoryWriteService
from aiive.worker.outbox_dto import (
    ClaimedJob,
    HandlerOutcome,
    HandlerResult,
    NonRetryableJobError,
    ValidatedExtractionSource,
)

logger = logging.getLogger(__name__)

SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})
LEASE_DURATION = 120  # seconds


def _get_llm_client() -> LLMClient:
    return LLMClient(
        base_url=settings.aiive_llm_base_url,
        api_key=settings.aiive_llm_api_key,
        default_model=settings.aiive_llm_model,
        timeout_seconds=settings.aiive_llm_timeout_seconds,
    )


# ============================================================================
# memory_extraction (three-phase)
# ============================================================================


def handle_memory_extraction(claimed: ClaimedJob) -> HandlerResult:
    """三阶段异步记忆提取 Handler。"""
    payload = claimed.payload
    user_message: str = payload.get("user_message", "")
    reply: str = payload.get("reply", "")
    thread_id: str = payload.get("thread_id", "")
    source_turn_record_id: str = payload.get("source_turn_record_id", "")
    source_turn_id: str = payload.get("source_turn_id", "")
    source_event_ids: list[str] = payload.get("source_event_ids", [])

    if MemoryExtractionPolicy.should_skip_system_message(user_message):
        return HandlerResult(HandlerOutcome.COMPLETED, "system_message_skipped")

    # ════════════════════════════════════════════════
    # Phase A: 短事务 —— 幂等预检查 + source Turn 验证
    # ════════════════════════════════════════════════
    db_a = SessionLocal()
    try:
        # A1: 验证 source Turn + schema_version
        validated_source = _validate_source_turn(
            db_a, source_turn_record_id, thread_id, claimed,
        )

        # A2: IngestionRun 幂等创建 + 状态检查
        irun = _resolve_ingestion_run(
            db_a, source_turn_record_id, claimed.claim_token,
        )

        if irun.decision == "already_succeeded":
            db_a.rollback()
            return HandlerResult(HandlerOutcome.COMPLETED, "already_succeeded",
                                 ingestion_run_id=irun.run_id)

        if irun.decision == "deadletter":
            db_a.rollback()
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "run_deadletter",
                                 ingestion_run_id=irun.run_id,
                                 terminal_reason="ingestion_run_deadletter")

        if irun.decision == "busy":
            db_a.rollback()
            return HandlerResult(
                HandlerOutcome.RETRY_LATER,
                reason="running_with_valid_token",
                retry_available_at=irun.lease_expires_at,
            )

        # acquired or takeover → commit running
        db_a.commit()
        execution_token = claimed.claim_token
        run_id = irun.run_id

    except NonRetryableJobError as e:
        db_a.rollback()
        return HandlerResult(HandlerOutcome.NON_RETRYABLE, str(e),
                             terminal_reason="source_turn_validation_failed")
    except Exception as e:
        db_a.rollback()
        logger.exception("Phase A failed")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"Phase A failed: {e}")
    finally:
        db_a.close()

    # ════════════════════════════════════════════════
    # Phase B: LLM 提取（事务外，无 DB Session）
    # ════════════════════════════════════════════════
    try:
        llm = _get_llm_client()
        extractor = UnifiedMemoryExtractor(llm)
        proposals: list[MemoryProposal] = extractor.extract(
            user_message=user_message, reply=reply,
            trace_id=claimed.trace_id, thread_id=thread_id,
        )
    except Exception as e:
        _mark_ingestion_failed_fencing(run_id, execution_token, str(e))
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"Extractor failed: {e}",
                             ingestion_run_id=run_id)

    # ════════════════════════════════════════════════
    # Phase C: 短事务 —— 双重 fencing + 写入 + 标记成功
    # ════════════════════════════════════════════════
    db_c = SessionLocal()
    try:
        # C1a: 双重 fencing — 先验证 OutboxJob
        outbox_c = db_c.query(OutboxJob).filter(
            OutboxJob.id == claimed.id,
        ).with_for_update().first()

        if outbox_c is None:
            db_c.rollback()
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "outbox_job_not_found",
                                 ingestion_run_id=run_id)

        if not (
            outbox_c.claim_token == claimed.claim_token
            and outbox_c.locked_by == claimed.worker_id
            and outbox_c.status == "running"
            and outbox_c.lease_expires_at is not None
            and outbox_c.lease_expires_at > datetime.now(timezone.utc)
        ):
            db_c.rollback()
            return HandlerResult(HandlerOutcome.CLAIM_LOST,
                                 "outbox_claim_expired_or_taken_over",
                                 ingestion_run_id=run_id)

        # C1b: 再验证 IngestionRun
        run_c = db_c.query(MemoryIngestionRun).filter(
            MemoryIngestionRun.id == run_id,
        ).with_for_update().first()

        if run_c is None or run_c.execution_token != execution_token:
            db_c.rollback()
            if run_c is not None and run_c.status == "succeeded":
                return HandlerResult(HandlerOutcome.COMPLETED, "already_succeeded_by_other",
                                     ingestion_run_id=run_id)
            if run_c is not None and run_c.status == "deadletter":
                return HandlerResult(HandlerOutcome.NON_RETRYABLE, "deadletter_by_other",
                                     ingestion_run_id=run_id,
                                     terminal_reason="ingestion_run_deadletter")
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "token_mismatch_or_taken_over",
                                 ingestion_run_id=run_id)

        if run_c.status != "running":
            db_c.rollback()
            if run_c.status == "succeeded":
                return HandlerResult(HandlerOutcome.COMPLETED, "already_succeeded",
                                     ingestion_run_id=run_id)
            if run_c.status == "deadletter":
                return HandlerResult(HandlerOutcome.NON_RETRYABLE, "run_deadletter",
                                     ingestion_run_id=run_id,
                                     terminal_reason="ingestion_run_deadletter")
            return HandlerResult(HandlerOutcome.CLAIM_LOST, f"run_not_running: {run_c.status}",
                                 ingestion_run_id=run_id)

        # C2: 构建 RunContext — Phase 2: 注入真实 Event.id
        run_ctx = RunContext(
            thread_id=validated_source.thread_id,
            trace_id=claimed.trace_id or "",
            source="outbox_worker",
            turn_id=validated_source.turn_id,
            turn_record_id=source_turn_record_id,
            execution_mode="system_best_effort",
            source_event_ids=source_event_ids,
        )

        writer = MemoryWriteService(db_c)

        if not proposals:
            affected_empty = db_c.query(MemoryIngestionRun).filter(
                MemoryIngestionRun.id == run_id,
                MemoryIngestionRun.execution_token == execution_token,
                MemoryIngestionRun.status == "running",
            ).update({
                MemoryIngestionRun.status: "succeeded",
                MemoryIngestionRun.proposal_count: 0,
                MemoryIngestionRun.completed_at: datetime.now(timezone.utc),
            }, synchronize_session=False)
            if affected_empty == 0:
                db_c.rollback()
                return HandlerResult(HandlerOutcome.CLAIM_LOST,
                                     "empty_result_fencing_violation",
                                     ingestion_run_id=run_id)
            db_c.commit()
            return HandlerResult(HandlerOutcome.COMPLETED, "empty_extraction",
                                 ingestion_run_id=run_id)

        # C3: 注入 Phase 2 provenance 到 proposal → 原子写入批次
        for p in proposals:
            p.source_turn_id = source_turn_id
            p.source_turn_record_id = source_turn_record_id
            p.source_event_ids = list(source_event_ids)
            p.execution_mode = "system_best_effort"
        writer.write_batch(proposals=proposals, ingestion_run=run_c, run_context=run_ctx)

        # C4: 标记 succeeded
        affected = db_c.query(MemoryIngestionRun).filter(
            MemoryIngestionRun.id == run_id,
            MemoryIngestionRun.execution_token == execution_token,
            MemoryIngestionRun.status == "running",
        ).update({
            MemoryIngestionRun.status: "succeeded",
            MemoryIngestionRun.proposal_count: len(proposals),
            MemoryIngestionRun.completed_at: datetime.now(timezone.utc),
        }, synchronize_session=False)
        if affected == 0:
            db_c.rollback()
            return HandlerResult(HandlerOutcome.CLAIM_LOST,
                                 "fencing_violation_in_finalize",
                                 ingestion_run_id=run_id)

        db_c.commit()
        return HandlerResult(HandlerOutcome.COMPLETED, f"written_{len(proposals)}",
                             ingestion_run_id=run_id)

    except MemoryBatchWriteError as e:
        db_c.rollback()
        _mark_ingestion_failed_fencing(run_id, execution_token, str(e))
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"Batch write failed: {e}",
                             ingestion_run_id=run_id)
    except Exception as e:
        db_c.rollback()
        _mark_ingestion_failed_fencing(run_id, execution_token, str(e))
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"Phase C failed: {e}",
                             ingestion_run_id=run_id)
    finally:
        db_c.close()


# ============================================================================
# core_memory_refresh (unified interface)
# ============================================================================


def handle_core_memory_refresh(claimed: ClaimedJob) -> HandlerResult:
    """core_memory_refresh 使用统一 Handler 接口。"""
    payload = claimed.payload
    memory_id: str = payload.get("memory_id", "")
    record_version: int = payload.get("record_version", 0)

    db = SessionLocal()
    try:
        from aiive.memory.core_memory_projection import CoreMemoryProjection
        from aiive.memory.recall_config import RecallConfig

        CoreMemoryProjection.refresh(db, memory_id, record_version, RecallConfig())
        db.commit()
        return HandlerResult(HandlerOutcome.COMPLETED, "core_memory_refreshed")
    except Exception as e:
        db.rollback()
        logger.exception("core_memory_refresh failed")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, str(e))
    finally:
        db.close()


# ============================================================================
# Projection stubs (保留兼容，但不在 allowlist 中所以不会被调用)
# ============================================================================


def handle_memory_vector_upsert(_claimed: ClaimedJob) -> HandlerResult:
    logger.warning("memory_vector_upsert: not in allowlist, should not be called")
    return HandlerResult(HandlerOutcome.NON_RETRYABLE, "unsupported_handler",
                         terminal_reason="unsupported_handler")


def handle_memory_vector_delete(_claimed: ClaimedJob) -> HandlerResult:
    return HandlerResult(HandlerOutcome.NON_RETRYABLE, "unsupported_handler",
                         terminal_reason="unsupported_handler")


def handle_memory_markdown_project(_claimed: ClaimedJob) -> HandlerResult:
    return HandlerResult(HandlerOutcome.NON_RETRYABLE, "unsupported_handler",
                         terminal_reason="unsupported_handler")


def handle_memory_cache_invalidate(_claimed: ClaimedJob) -> HandlerResult:
    return HandlerResult(HandlerOutcome.NON_RETRYABLE, "unsupported_handler",
                         terminal_reason="unsupported_handler")


# ============================================================================
# Registration
# ============================================================================


def register_all(registry: HandlerRegistry) -> None:
    registry.register("memory_extraction", handle_memory_extraction,
                       supported_schema_versions=frozenset({1}))
    registry.register("core_memory_refresh", handle_core_memory_refresh,
                       supported_schema_versions=frozenset({1}))


# ============================================================================
# Internal helpers
# ============================================================================


def _validate_source_turn(
    db: Session,
    source_turn_record_id: str,
    expected_thread_id: str,
    claimed: ClaimedJob,
) -> ValidatedExtractionSource:
    """Phase A：验证 source Turn 存在且有效 + schema_version。"""
    if claimed.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise NonRetryableJobError(
            f"Unsupported schema_version={claimed.schema_version}"
        )

    if not source_turn_record_id:
        raise NonRetryableJobError("missing source_turn_record_id in payload")

    turn = db.get(TurnRecord, source_turn_record_id)
    if turn is None:
        raise NonRetryableJobError(f"TurnRecord not found: {source_turn_record_id}")

    if turn.status != "completed":
        raise NonRetryableJobError(f"TurnRecord not completed: status={turn.status}")

    if turn.thread_id != expected_thread_id:
        raise NonRetryableJobError(
            f"Thread mismatch: expected={expected_thread_id}, actual={turn.thread_id}"
        )

    if not turn.turn_id:
        raise NonRetryableJobError("TurnRecord.turn_id is empty")

    return ValidatedExtractionSource(
        turn_record_id=turn.id,
        thread_id=turn.thread_id,
        turn_id=turn.turn_id,
    )


def _resolve_ingestion_run(
    db: Session, source_turn_record_id: str, claim_token: str,
):
    """Phase A：创建/读取 IngestionRun 并决定执行权。"""
    from aiive.worker.outbox_dto import IngestionRunResolution

    stmt = pg_insert(MemoryIngestionRun).values(
        id=str(__import__("uuid").uuid4()),
        source_turn_record_id=source_turn_record_id,
        extractor_name="UnifiedMemoryExtractor",
        extractor_version="1.0",
        status="pending",
    ).on_conflict_do_nothing(
        index_elements=["source_turn_record_id", "extractor_name", "extractor_version"]
    )
    db.execute(stmt)

    run = db.query(MemoryIngestionRun).filter(
        MemoryIngestionRun.source_turn_record_id == source_turn_record_id,
        MemoryIngestionRun.extractor_name == "UnifiedMemoryExtractor",
        MemoryIngestionRun.extractor_version == "1.0",
    ).with_for_update().first()

    if run is None:
        raise NonRetryableJobError("Failed to create/resolve IngestionRun")

    if run.status == "succeeded":
        return IngestionRunResolution(decision="already_succeeded", run_id=run.id)

    if run.status == "deadletter":
        return IngestionRunResolution(decision="deadletter", run_id=run.id)

    if run.status == "running" and run.execution_token:
        outbox_job = db.query(OutboxJob).filter(
            OutboxJob.claim_token == run.execution_token,
            OutboxJob.status == "running",
        ).first()

        if outbox_job and outbox_job.lease_expires_at:
            if outbox_job.lease_expires_at > datetime.now(timezone.utc):
                return IngestionRunResolution(
                    decision="busy",
                    lease_expires_at=outbox_job.lease_expires_at,
                )

    was_running = (run.status == "running")
    previous_status = run.status

    run.status = "running"
    run.execution_token = claim_token
    run.started_at = datetime.now(timezone.utc)
    # 接管时清空旧 completed_at 和 error_message
    if was_running:
        run.completed_at = None
        run.error_message = None
    db.flush()

    return IngestionRunResolution(
        decision="acquired" if not was_running else "takeover",
        run_id=run.id,
        previous_status=previous_status,
    )


def _mark_ingestion_failed_fencing(
    run_id: str, execution_token: str, error: str,
) -> None:
    """独立短事务：清理 running IngestionRun。"""
    db_m = SessionLocal()
    try:
        affected = db_m.query(MemoryIngestionRun).filter(
            MemoryIngestionRun.id == run_id,
            MemoryIngestionRun.execution_token == execution_token,
            MemoryIngestionRun.status == "running",
        ).update({
            MemoryIngestionRun.status: "failed",
            MemoryIngestionRun.execution_token: None,
            MemoryIngestionRun.error_message: error[:500],
            MemoryIngestionRun.completed_at: datetime.now(timezone.utc),
        }, synchronize_session=False)
        db_m.commit()
        if affected == 0:
            logger.warning(
                "_mark_ingestion_failed_fencing: no row matched "
                + "run_id=%s token=%s", run_id, execution_token,
            )
    except Exception:
        logger.exception("_mark_ingestion_failed_fencing itself failed")
    finally:
        db_m.close()
