"""Phase 0.5B: Outbox handlers — unified (ClaimedJob) -> HandlerResult interface."""

from __future__ import annotations

import json as _json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiive.worker.handler_registry import HandlerRegistry

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aiive.config import settings
from aiive.context.run_context import RunContext, RUN_CTX_OUTBOX_WORKER
from aiive.core.llm_client import LLMClient
from aiive.db.base import SessionLocal
from aiive.db.models import (
    CheckpointRun,
    CompactionInput,
    CompactionRun,
    Epoch,
    EpochCheckpoint,
    EpochCompactionInput,
    Event,
    MemoryIngestionRun,
    MemoryMaintenanceAction,
    MemoryMaintenanceBatch,
    MemoryMaintenanceInput,
    MemoryMaintenanceRun,
    OutboxJob,
    Segment,
    SegmentSummary,
    Task,
    TurnRecord,
)
from aiive.memory.extraction_policy import MemoryExtractionPolicy
from aiive.memory.memory_extractor import UnifiedMemoryExtractor
from aiive.memory.memory_types import MemoryProposal
from aiive.memory.memory_write_service import MemoryBatchWriteError, MemoryWriteService
from aiive.prompts import get_prompt_registry
from aiive.retrieval.index_rebuild import handle_retrieval_index_rebuild
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


def _claim_matches(outbox: "OutboxJob", claimed: "ClaimedJob", now: datetime) -> bool:
    """claim 是否仍有效（时区安全的 lease 比较）。

    SQLite 不保留时区信息，重载后的 lease_expires_at 为 naive datetime；
    与 Postgres（aware）统一按 UTC 比较，避免 TypeError。
    """
    lease = outbox.lease_expires_at
    if lease is None:
        return False
    if lease.tzinfo is None:
        lease = lease.replace(tzinfo=timezone.utc)
    return (
        outbox.claim_token == claimed.claim_token
        and outbox.locked_by == claimed.worker_id
        and outbox.status == "running"
        and lease > now
    )


def _lease_active(lease_expires_at: datetime | None, now: datetime) -> bool:
    """租约是否仍有效（时区安全比较，与 _claim_matches 同口径）。

    SQLite 不保留时区信息，读回的 lease_expires_at 为 naive datetime；
    与 Postgres（aware）统一按 UTC 比较，避免 TypeError。
    """
    if lease_expires_at is None:
        return False
    lease = lease_expires_at
    if lease.tzinfo is None:
        lease = lease.replace(tzinfo=timezone.utc)
    return lease > now


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
    message_source = payload.get("message_source")
    reply: str = payload.get("reply", "")
    thread_id: str = payload.get("thread_id", "")
    source_turn_record_id: str = payload.get("source_turn_record_id", "")
    source_turn_id: str = payload.get("source_turn_id", "")
    source_event_ids: list[str] = payload.get("source_event_ids", [])
    assistant_event_id: str = payload.get("assistant_event_id", "")

    if MemoryExtractionPolicy.should_skip_system_message(user_message, message_source):
        return HandlerResult(HandlerOutcome.COMPLETED, "non_user_message_skipped")

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
            source_event_ids=source_event_ids or None,
            assistant_event_ids=[assistant_event_id] if assistant_event_id else None,
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

        if not _claim_matches(outbox_c, claimed, datetime.now(timezone.utc)):
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
            source=RUN_CTX_OUTBOX_WORKER,
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
        # Phase B 抽取时已按 payload 的真实事件构建 evidence 与 source_event_ids；
        # 此处仅补充 turn 溯源字段。source_event_ids 只在 payload 非空时覆盖，
        # 避免把 Phase B 使用的 trace_id 兜底清空导致 evidence 与 source_event_ids 不一致。
        for p in proposals:
            p.source_turn_id = source_turn_id
            p.source_turn_record_id = source_turn_record_id
            if source_event_ids:
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
# reminder_delivery
# ============================================================================


def _remind_alert_succeeded(db: Session, turn_id: str, expected_reminder_id: str) -> bool:
    """校验当前提醒 Turn 是否真实成功调用了 remind_alert 且目标 ID 匹配。

    只有 LLM 实际执行该工具（tool_call 与 completed 的 tool_result 成对出现、
    且参数中的 reminder_id 与本次到期事件一致）才视为已警报，禁止伪造卡片或状态。
    """
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    results: dict[str, str] = {}
    events = db.query(Event).filter(
        Event.turn_id == turn_id,
        Event.event_type.in_(["tool_call", "tool_result"]),
    ).all()
    for event in events:
        payload = event.payload or {}
        tool_call_id = str(payload.get("tool_call_id", "") or "")
        if event.event_type == "tool_call":
            calls[tool_call_id] = (str(payload.get("name", "")), payload.get("params") or {})
        else:
            results[tool_call_id] = str(payload.get("status", ""))
    for tool_call_id, (name, params) in calls.items():
        if name != "remind_alert":
            continue
        if str(params.get("reminder_id", "")) != str(expected_reminder_id):
            continue
        if results.get(tool_call_id) == "completed":
            return True
    return False


def handle_reminder_delivery(claimed: ClaimedJob) -> HandlerResult:
    """投递到期提醒：使用确定性 Turn 唤醒 Agent，成功后完成 Task。"""
    import uuid as _uuid

    payload = claimed.payload
    task_id = str(payload.get("task_id", ""))
    thread_id = str(payload.get("thread_id", ""))
    title = str(payload.get("title", ""))
    content = str(payload.get("content", "")) or title
    operation_id = str(payload.get("operation_id", "")) or f"reminder_delivery:{task_id}"
    if not task_id or not thread_id or not title:
        return HandlerResult(
            HandlerOutcome.NON_RETRYABLE,
            "提醒投递参数不完整",
            terminal_reason="invalid_reminder_payload",
        )

    db_a = SessionLocal()
    try:
        outbox = db_a.query(OutboxJob).filter(OutboxJob.id == claimed.id).with_for_update().first()
        if outbox is None or not _claim_matches(outbox, claimed, datetime.now(timezone.utc)):
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "提醒投递 claim 已失效")
        task = db_a.query(Task).filter(Task.id == task_id).with_for_update().first()
        if task is None:
            return HandlerResult(
                HandlerOutcome.NON_RETRYABLE,
                "提醒任务不存在",
                terminal_reason="reminder_task_not_found",
            )
        if task.status == "completed":
            return HandlerResult(HandlerOutcome.COMPLETED, "提醒已由其他执行完成")
        if task.status == "cancelled":
            return HandlerResult(HandlerOutcome.COMPLETED, "提醒已取消")
        if task.status != "dispatching":
            return HandlerResult(
                HandlerOutcome.NON_RETRYABLE,
                f"提醒任务状态无效: {task.status}",
                terminal_reason="invalid_reminder_task_status",
            )
        reminder_event = db_a.query(Event).filter(
            Event.event_type == "reminder_created",
            Event.trace_id == task_id,
        ).order_by(Event.created_at.desc()).first()
        if reminder_event is None:
            return HandlerResult(
                HandlerOutcome.NON_RETRYABLE,
                "提醒缺少关联的 reminder_created 事件",
                terminal_reason="reminder_event_not_found",
            )
        reminder_id = reminder_event.id
        db_a.commit()
    finally:
        db_a.close()

    turn_id = str(_uuid.uuid5(_uuid.NAMESPACE_URL, operation_id))
    message = get_prompt_registry().render(
        "runtime.reminder_delivery",
        reminder_id=reminder_id,
        title=title,
        content=content,
    ).content
    try:
        from aiive.core.llm_client import default_llm_client
        from aiive.runtime.turn_execution import TurnConflictError, TurnExecutionService
    except Exception as exc:
        logger.exception("提醒 Agent 运行时加载异常: task_id=%s", task_id)
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"提醒 Agent 运行时加载异常: {exc}")

    try:
        result = TurnExecutionService(
            default_llm_client(), message_source="runtime_event",
        ).execute_turn(message=message, thread_id=thread_id, turn_id=turn_id)
    except TurnConflictError as exc:
        # turn_id 由 operation_id 确定性生成：Turn 已 completed 时重跑只会命中
        # 缓存（turn_cached）。不重跑 Turn，直接进入下方校验阶段读缓存 Turn 的
        # 事件流校验 _remind_alert_succeeded，由其决定完成或降级。
        if str(exc) == "turn_cached":
            result = {}
        else:
            logger.exception("提醒 Agent Turn 冲突: task_id=%s", task_id)
            return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"提醒 Agent Turn 冲突: {exc}")
    except Exception as exc:
        logger.exception("提醒 Agent 执行异常: task_id=%s", task_id)
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"提醒 Agent 执行异常: {exc}")

    turn_error = str(result.get("error") or "")
    if turn_error == "turn_in_progress":
        return HandlerResult(HandlerOutcome.RETRY_LATER, "提醒 Turn 正在执行")
    if turn_error == "turn_cached_no_payload":
        # Turn 已 completed 但缓存无 payload：重试不可能改变结果，
        # 继续进入校验/降级路径（event_id 由下方 DB 回查兜底）。
        result = {}
    elif turn_error:
        return HandlerResult(
            HandlerOutcome.RETRYABLE_ERROR,
            f"提醒 Agent 执行失败: {turn_error}",
        )
    event_id = str(result.get("event_id", "") or "")
    if not event_id:
        db_event = SessionLocal()
        try:
            assistant_event = db_event.query(Event).filter(
                Event.thread_id == thread_id,
                Event.turn_id == turn_id,
                Event.event_type == "llm_response",
            ).order_by(Event.turn_event_index.desc(), Event.id.desc()).first()
            event_id = str(assistant_event.id) if assistant_event is not None else ""
        finally:
            db_event.close()
    if not event_id:
        # Turn 已 completed（确定性 turn_id），缺 llm_response 同样不会因重试改变；
        # 继续进入校验阶段，由 remind_alert 校验决定完成或降级，不再空转重试。
        logger.warning(
            "提醒 Turn 缺少可验证的 llm_response event_id，进入校验/降级路径: task_id=%s",
            task_id,
        )

    degraded = False
    db_c = SessionLocal()
    try:
        outbox = db_c.query(OutboxJob).filter(OutboxJob.id == claimed.id).with_for_update().first()
        if outbox is None or not _claim_matches(outbox, claimed, datetime.now(timezone.utc)):
            db_c.rollback()
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "提醒完成阶段 claim 已失效")
        task = db_c.query(Task).filter(Task.id == task_id).with_for_update().first()
        if task is None:
            db_c.rollback()
            return HandlerResult(
                HandlerOutcome.NON_RETRYABLE,
                "提醒任务不存在",
                terminal_reason="reminder_task_not_found",
            )
        if task.status == "cancelled":
            db_c.commit()
            return HandlerResult(HandlerOutcome.COMPLETED, "提醒投递期间已取消")
        if task.status == "dispatching":
            alert_verified = _remind_alert_succeeded(db_c, turn_id, reminder_id)
            if not alert_verified:
                # 「LLM 未调用 remind_alert」对确定性 turn_id 是重试不可改变的
                # 终态（重跑只会命中 turn 缓存）。不再空转重试直至 deadletter
                # （用户全程无感知），降级为兜底通知：写 notification_created
                # 事件（含 task 信息）并完成任务，保证用户可感知提醒已到期。
                degraded = True
                db_c.add(Event(
                    trace_id=claimed.trace_id or task_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    event_type="notification_created",
                    payload={
                        "task_id": task_id,
                        "task_type": task.task_type,
                        "title": title,
                        "content": content,
                        "message": content,
                        "status": "alerting",
                        "degraded": True,
                        "degraded_reason": "remind_alert_not_invoked",
                    },
                ))
            reminder_event = db_c.query(Event).filter(
                Event.event_type == "reminder_created",
                Event.trace_id == task_id,
            ).order_by(Event.created_at.desc()).first()
            if reminder_event is None:
                reminder_event = Event(
                    trace_id=task_id,
                    thread_id=thread_id,
                    event_type="reminder_created",
                    payload={
                        "task_id": task_id,
                        "title": title,
                        "content": content,
                        "status": "alerting",
                    },
                )
                db_c.add(reminder_event)
            else:
                reminder_payload = dict(reminder_event.payload or {})
                reminder_payload["status"] = "alerting"
                reminder_event.payload = reminder_payload
            if alert_verified:
                db_c.add(Event(
                    trace_id=claimed.trace_id or task_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    event_type="reminder_triggered",
                    payload={
                        "task_id": task_id,
                        "task_type": task.task_type,
                        "title": title,
                        "turn_id": turn_id,
                    },
                ))
            task.status = "completed"
        elif task.status != "completed":
            db_c.rollback()
            return HandlerResult(HandlerOutcome.CLAIM_LOST, f"提醒状态已变化: {task.status}")
        db_c.commit()
    except Exception as exc:
        db_c.rollback()
        logger.exception("提醒完成状态持久化失败: task_id=%s", task_id)
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"提醒完成状态持久化失败: {exc}")
    finally:
        db_c.close()

    try:
        from aiive.api.routes_notifications import broadcast_pending_count

        db_notification = SessionLocal()
        try:
            broadcast_pending_count(db_notification)
        finally:
            db_notification.close()
    except Exception:
        logger.exception("提醒通知状态推送失败: task_id=%s", task_id)

    if degraded:
        # 降级路径：无有效 Agent 回复可推送，改为向来源线程广播兜底通知，
        # 前端据此提示提醒已到期。返回 COMPLETED（降级成功），不再重试。
        try:
            from aiive.api.ws_manager import ws_manager

            ws_manager.broadcast_to_thread_sync(
                thread_id,
                "reminder_fallback_notification",
                {
                    "task_id": task_id,
                    "title": title,
                    "content": content,
                    "thread_id": thread_id,
                    "degraded_reason": "remind_alert_not_invoked",
                },
            )
        except Exception:
            logger.exception("提醒降级通知 WebSocket 推送失败: task_id=%s", task_id)
        return HandlerResult(
            HandlerOutcome.COMPLETED,
            "提醒 Turn 未调用 remind_alert，已降级为兜底通知",
        )

    try:
        from aiive.api.ws_manager import ws_manager

        ws_manager.broadcast_to_thread_sync(
            thread_id,
            "new_message",
            {
                "event_id": event_id,
                "reply": result.get("reply", ""),
                "thread_id": result.get("thread_id", thread_id),
                "trace_id": result.get("trace_id", ""),
                "action_cards": result.get("action_cards", []),
                # 后台提醒 turn 同样带上工具调用记录，使前端能显示 remind_alert 等工具调用框
                "tool_calls": result.get("tool_calls", []),
            },
        )
    except Exception:
        logger.exception("提醒回复 WebSocket 推送失败: task_id=%s", task_id)

    return HandlerResult(HandlerOutcome.COMPLETED, "提醒 Agent 回复已完成")


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


def handle_memory_vector_refresh(claimed: ClaimedJob) -> HandlerResult:
    """按 MemoryRecord 真相源状态刷新或删除 pgvector 投影。"""
    memory_id = str(claimed.payload.get("memory_id", ""))
    record_version = int(claimed.payload.get("record_version", 0))
    if not memory_id or record_version <= 0:
        return HandlerResult(
            HandlerOutcome.NON_RETRYABLE,
            "向量投影任务缺少 memory_id 或 record_version",
            terminal_reason="invalid_payload",
        )
    db = SessionLocal()
    try:
        from aiive.memory.vector_projection import MemoryVectorProjectionService

        result = MemoryVectorProjectionService(db).refresh(memory_id, record_version)
        db.commit()
        return HandlerResult(HandlerOutcome.COMPLETED, result)
    except Exception as exc:
        db.rollback()
        logger.exception("memory_vector_refresh failed")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, str(exc))
    finally:
        db.close()


def handle_memory_markdown_project(claimed: ClaimedJob) -> HandlerResult:
    """从当前 PostgreSQL 真相源重建 Markdown 和 JSON 文件投影。"""
    if not settings.aiive_memory_file_projection_enabled:
        return HandlerResult(HandlerOutcome.COMPLETED, "记忆文件投影已关闭")
    if not claimed.payload.get("memory_id") and not claimed.payload.get("forget_operation_id"):
        return HandlerResult(
            HandlerOutcome.NON_RETRYABLE,
            "文件投影任务缺少触发来源",
            terminal_reason="invalid_payload",
        )

    db = SessionLocal()
    try:
        from pathlib import Path

        from aiive.memory.projection import MemoryProjection

        result = MemoryProjection(db).write_projection(
            Path(settings.aiive_memory_file_projection_dir),
        )
        db.rollback()
        return HandlerResult(
            HandlerOutcome.COMPLETED,
            f"文件投影已生成 {result['record_count']} 条记录，snapshot={result['snapshot_id']}",
        )
    except Exception as exc:
        db.rollback()
        logger.exception("memory_markdown_project failed")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, str(exc))
    finally:
        db.close()


def handle_retrieval_index_refresh(claimed: ClaimedJob) -> HandlerResult:
    """retrieval_index_refresh Handler：对 active + building generation 刷新单个 source。

    幂等：operation_id 以 source 为粒度（retrieval_refresh:{type}:{id}），
    多次变更合并为一次刷新；upsert_entry 内部按 source_version fencing 防旧覆盖新。
    """
    payload = claimed.payload
    source_type: str = payload.get("source_type", "")
    source_id: str = payload.get("source_id", "")
    event_type: str = payload.get("event_type", "")
    if not source_type or not source_id:
        return HandlerResult(HandlerOutcome.COMPLETED, "missing_source")

    db = SessionLocal()
    try:
        from aiive.retrieval.indexing_service import refresh_source
        upserted, tombstoned = refresh_source(db, source_type, source_id, event_type)
        db.commit()
        return HandlerResult(
            HandlerOutcome.COMPLETED,
            f"refreshed upserted={upserted} tombstoned={tombstoned}",
        )
    except Exception as e:
        db.rollback()
        logger.exception("retrieval_index_refresh failed")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, str(e))
    finally:
        db.close()


# ============================================================================
# Registration
# ============================================================================


# ============================================================================
# Phase 4: memory_maintenance（Daily Dream 三阶段）
# ============================================================================


def _parse_dt(value: str | None) -> datetime | None:
    """解析冻结快照中的 ISO 时间字符串，naive 值按 UTC 补齐时区。

    SQLite 读回的 datetime 为 naive，其 isoformat 无时区偏移；补齐 UTC 后
    可与 planner 中 aware 的 `now` 安全比较，避免 naive/aware 混比报错。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_MAINTENANCE_LANE_ORDER: tuple[str, ...] = (
    "changed",
    "expired_ephemeral",
    "candidate_due",
    "sleep_due",
)


def _seed_lane_time(seed: Any, lane: str) -> Any:
    """seed 在某 lane 的游标时间维（第 1 点）。"""
    if lane == "changed":
        return seed.updated_at
    if lane == "expired_ephemeral":
        return seed.valid_to
    if lane == "candidate_due":
        return seed.created_at
    if lane == "sleep_due":
        return seed.last_accessed_at or seed.observed_at or seed.created_at
    return None


def _maintenance_lane_cursor(db: Session, run: MemoryMaintenanceRun, lane: str):
    """返回某 lane 的续跑游标 (cursor_time, cursor_id)。

    - changed lane 用 Run 级 `cursor_updated_at`/`cursor_id`；
    - 其余 lane 用本 Run 内该 lane 最近已完成 Batch 的 `cursor_end_*`。
    """
    if lane == "changed":
        return run.cursor_updated_at, run.cursor_id
    last = (
        db.query(MemoryMaintenanceBatch)
        .filter(
            MemoryMaintenanceBatch.run_id == run.id,
            MemoryMaintenanceBatch.candidate_lane == lane,
            MemoryMaintenanceBatch.status == "done",
        )
        .order_by(MemoryMaintenanceBatch.batch_no.desc())
        .first()
    )
    if last is None:
        return None, None
    return last.cursor_end_time, last.cursor_end_id


def _maintenance_lane_has_more(
    db: Session, store: Any, run: MemoryMaintenanceRun, lane: str, cfg: Any, now: datetime,
) -> bool:
    """判断某 lane 是否仍有未处理 seed。"""
    ct, cid = _maintenance_lane_cursor(db, run, lane)
    seeds = store.select_maintenance_seeds(
        lane,
        cutoff_updated_at=run.cutoff_updated_at,
        cursor_time=ct,
        cursor_id=cid,
        limit=1,
        now=now,
        ttl_days=cfg.candidate_ttl_days,
        cooling_days=cfg.sleep_cooling_days,
    )
    return len(seeds) > 0


def _freeze_input(_db: Session, batch_id: str, run_id: str, record: Any, input_role: str, seq: int, _now: Any, _cfg: Any, store: Any) -> MemoryMaintenanceInput:
    """冻结单条记忆快照为 MemoryMaintenanceInput。"""
    from aiive.memory.memory_mutation import hashes_for_record

    state_hash, decision_hash = hashes_for_record(record)
    protected, source_ids = store.is_user_required_protected(record.id)
    snapshot = {
        "content": record.content,
        "structured_value": record.structured_value,
        "content_hash": record.content_hash,
        "structured_value_hash": record.structured_value_hash,
        "lifecycle_state": record.lifecycle_state,
        "validity_state": record.validity_state,
        "confidence": record.confidence,
        "importance": record.importance,
        "retention_policy": record.retention_policy,
        "valid_to": record.valid_to.isoformat() if record.valid_to else None,
        "pinned": bool(record.pinned),
        "stability": record.stability,
        "stability_score": record.stability_score,
        "reinforce_count": record.reinforce_count,
        "last_reinforced_at": (
            record.last_reinforced_at.isoformat() if record.last_reinforced_at else None
        ),
        "observed_at": record.observed_at.isoformat() if record.observed_at else None,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "last_accessed_at": (
            record.last_accessed_at.isoformat() if record.last_accessed_at else None
        ),
        "evidence_count": store.count_independent_evidence(record.id),
    }
    return MemoryMaintenanceInput(
        run_id=run_id,
        batch_id=batch_id,
        input_sequence=seq,
        memory_record_id=record.id,
        input_role=input_role,
        record_version=record.record_version,
        record_state_hash=state_hash,
        decision_hash=decision_hash,
        user_required_protected=protected,
        user_required_source_ids=source_ids,
        canonical_key=record.canonical_key,
        scope_type=record.scope_type,
        scope_id=record.scope_id,
        snapshot_json=snapshot,
    )


def _frozen_input_from_row(mi: MemoryMaintenanceInput):
    """从已冻结的 MemoryMaintenanceInput 还原为 planner 的 FrozenInput。"""
    from aiive.memory.memory_maintenance_planner import FrozenInput

    s = mi.snapshot_json or {}
    return FrozenInput(
        input_id=mi.id,
        input_sequence=mi.input_sequence,
        memory_record_id=mi.memory_record_id,
        input_role=mi.input_role,
        record_version=mi.record_version,
        canonical_key=mi.canonical_key,
        scope_type=mi.scope_type,
        scope_id=mi.scope_id,
        user_required_protected=mi.user_required_protected,
        user_required_source_ids=mi.user_required_source_ids or [],
        content=s.get("content") or "",
        structured_value=s.get("structured_value"),
        content_hash=s.get("content_hash"),
        structured_value_hash=s.get("structured_value_hash"),
        lifecycle_state=s.get("lifecycle_state") or "",
        validity_state=s.get("validity_state") or "",
        confidence=s.get("confidence") or 0.5,
        importance=s.get("importance") or 0.5,
        retention_policy=s.get("retention_policy") or "normal",
        valid_to=_parse_dt(s.get("valid_to")),
        pinned=bool(s.get("pinned")),
        stability=s.get("stability") or "contextual",
        stability_score=s.get("stability_score"),
        reinforce_count=s.get("reinforce_count") or 0,
        last_reinforced_at=_parse_dt(s.get("last_reinforced_at")),
        observed_at=_parse_dt(s.get("observed_at")) or _parse_dt(s.get("created_at")) or datetime.now(timezone.utc),
        created_at=_parse_dt(s.get("created_at")) or datetime.now(timezone.utc),
        last_accessed_at=_parse_dt(s.get("last_accessed_at")),
        evidence_count=s.get("evidence_count") or 0,
    )


def _create_batch_and_freeze(db: Session, run: MemoryMaintenanceRun, lane: str, cfg: Any, now: datetime) -> Any:
    """为某 lane 选 seed + 扩展 neighbor，新建 frozen Batch 并冻结 Input。

    返回 Batch；若无 seed（并发竞态）返回 None。
    """
    from aiive.memory.memory_store import MemoryStore

    store = MemoryStore(db)
    ct, cid = _maintenance_lane_cursor(db, run, lane)
    seeds = store.select_maintenance_seeds(
        lane,
        cutoff_updated_at=run.cutoff_updated_at,
        cursor_time=ct,
        cursor_id=cid,
        limit=cfg.max_maintenance_batch,
        now=now,
        ttl_days=cfg.candidate_ttl_days,
        cooling_days=cfg.sleep_cooling_days,
    )
    if not seeds:
        return None

    max_no = db.query(MemoryMaintenanceBatch.batch_no).filter(
        MemoryMaintenanceBatch.run_id == run.id,
    ).order_by(MemoryMaintenanceBatch.batch_no.desc()).first()
    batch_no = (max_no[0] + 1) if max_no else 0

    batch = MemoryMaintenanceBatch(
        run_id=run.id,
        batch_no=batch_no,
        candidate_lane=lane,
        status="frozen",
        lane_cursor_time=ct,
        lane_cursor_id=cid,
        cursor_start_time=ct,
        cursor_start_id=cid,
    )
    db.add(batch)
    db.flush()

    inputs: list[MemoryMaintenanceInput] = []
    seq = 0
    seen: set[str] = set()
    for seed in seeds:
        # 若该 seed 已作为前一条 seed 的邻居被冻结，则跳过，避免
        # 同批次内 (batch_id, memory_record_id) 唯一约束冲突（如互为重复的候选）。
        if seed.id in seen:
            continue
        inputs.append(_freeze_input(db, batch.id, run.id, seed, "seed", seq, now, cfg, store))
        seq += 1
        seen.add(seed.id)
        for nb in store.expand_maintenance_neighbors(seed):
            if nb.id in seen:
                continue
            seen.add(nb.id)
            inputs.append(_freeze_input(db, batch.id, run.id, nb, "neighbor", seq, now, cfg, store))
            seq += 1

    for inp in inputs:
        db.add(inp)
    batch.candidate_count = len(inputs)

    # 续跑游标：本批最后一条 seed 的 (lane_time, id)
    last_seed = seeds[-1]
    batch.cursor_end_time = _seed_lane_time(last_seed, lane)
    batch.cursor_end_id = last_seed.id
    db.flush()
    return batch


def _ensure_batch_and_freeze(db: Session, run: MemoryMaintenanceRun, cfg: Any, now: datetime) -> Any:
    """恢复优先：先接管未完成 Batch；否则按 lane 选下一批并冻结。

    若所有 lane 均无剩余候选 → 标记 Run succeeded 并返回 None。
    """
    from aiive.memory.memory_store import MemoryStore

    unfinished = (
        db.query(MemoryMaintenanceBatch)
        .filter(
            MemoryMaintenanceBatch.run_id == run.id,
            MemoryMaintenanceBatch.status.in_(["frozen", "planned", "applying"]),
        )
        .order_by(MemoryMaintenanceBatch.batch_no.asc())
        .first()
    )
    if unfinished is not None:
        return unfinished

    store = MemoryStore(db)
    for lane in _MAINTENANCE_LANE_ORDER:
        if _maintenance_lane_has_more(db, store, run, lane, cfg, now):
            batch = _create_batch_and_freeze(db, run, lane, cfg, now)
            if batch is not None:
                return batch

    run.status = "succeeded"
    run.completed_at = now
    db.flush()
    return None


def _persist_actions(db: Session, batch: MemoryMaintenanceBatch, actions: Any, input_hash: str, plan_hash: str, cfg: Any, now: datetime) -> None:
    """B2：持久化确定性 Action（幂等），Batch frozen → planned。"""
    batch.input_hash = input_hash
    batch.plan_hash = plan_hash
    batch.action_count = len(actions)
    batch.status = "planned"
    batch.planned_at = now
    for a in actions:
        idem = (
            f"{batch.run_id}:{a.action_sequence}:{a.operation_group_id}"
            f":{a.subject_memory_record_id}:{a.action_type}:{cfg.policy_version}"
        )
        act = MemoryMaintenanceAction(
            run_id=batch.run_id,
            batch_id=batch.id,
            action_sequence=a.action_sequence,
            source_input_ids=a.source_input_ids,
            operation_group_id=a.operation_group_id,
            subject_memory_record_id=a.subject_memory_record_id,
            related_record_ids=a.related_record_ids,
            canonical_key=a.canonical_key,
            scope_type=a.scope_type,
            scope_id=a.scope_id,
            action_type=a.action_type,
            reason_code=a.reason_code,
            expected_record_version=a.expected_record_version,
            preconditions=a.preconditions,
            idempotency_key=idem,
            status="pending",
        )
        db.add(act)
    db.flush()


def _execute_batch(db: Session, batch: MemoryMaintenanceBatch, run: MemoryMaintenanceRun, _cfg: Any, now: datetime, trace_id: str) -> None:
    """Phase C：执行本 Batch 全部非终态 Action，原子 finalize + 推进游标。"""
    from aiive.memory.memory_lifecycle_service import MemoryLifecycleService

    actions = (
        db.query(MemoryMaintenanceAction)
        .filter(MemoryMaintenanceAction.batch_id == batch.id)
        .order_by(MemoryMaintenanceAction.action_sequence.asc())
        .all()
    )
    lifecycle = MemoryLifecycleService(db)
    stale_groups: set[str] = set()
    applied = skipped = noop = 0

    for act in actions:
        if act.status in ("applied", "skipped_stale", "no_op"):
            if act.status == "applied":
                applied += 1
            elif act.status == "no_op":
                noop += 1
            else:
                skipped += 1
            continue
        grp = act.operation_group_id or ""
        if grp in stale_groups:
            act.status = "skipped_stale"
            act.applied_at = now
            skipped += 1
            continue
        status = lifecycle.apply_maintenance_action(act, trace_id=trace_id)
        act.applied_at = now
        if status == "applied":
            act.status = "applied"
            applied += 1
        elif status == "no_op":
            act.status = "no_op"
            noop += 1
        else:
            act.status = "skipped_stale"
            skipped += 1
            stale_groups.add(grp)

    batch.applied_count = applied
    batch.skipped_stale_count = skipped
    batch.completed_at = now
    batch.status = "done"

    # Run 级统计聚合（跨 Batch 累计，避免三列恒为默认 0）
    run.candidate_count = (run.candidate_count or 0) + (batch.candidate_count or 0)
    run.applied_count = (run.applied_count or 0) + applied
    run.skipped_stale_count = (run.skipped_stale_count or 0) + skipped

    # 推进 Run 级 changed lane 游标
    if batch.candidate_lane == "changed" and batch.cursor_end_time is not None:
        run.cursor_updated_at = batch.cursor_end_time
        run.cursor_id = batch.cursor_end_id
    db.flush()


def _resolve_maintenance_run(
    db: Session, outbox_job_id: str, operation_id: str, claim_token: str, now: datetime,
) -> tuple[str, str]:
    """创建/接管 MemoryMaintenanceRun（一 OutboxJob 一 Run）。"""
    import uuid as _uuid

    prev = (
        db.query(MemoryMaintenanceRun)
        .filter(MemoryMaintenanceRun.status == "succeeded")
        .order_by(MemoryMaintenanceRun.completed_at.desc())
        .first()
    )
    prev_cutoff = prev.cutoff_updated_at if prev else None

    _insert_conflict_do_nothing(
        db, MemoryMaintenanceRun,
        {
            "id": str(_uuid.uuid4()),
            "outbox_job_id": outbox_job_id,
            "operation_id": operation_id,
            "scope_type": "all_user_memories",
            "cutoff_updated_at": now,
            "cursor_updated_at": prev_cutoff,
            "status": "running",
        },
        conflict_columns=["outbox_job_id"],
    )
    run = (
        db.query(MemoryMaintenanceRun)
        .filter(MemoryMaintenanceRun.outbox_job_id == outbox_job_id)
        .with_for_update()
        .first()
    )
    if run is None:
        raise NonRetryableJobError("无法解析 maintenance Run")

    if run.status == "succeeded":
        return "already_succeeded", run.id
    if run.status == "deadletter":
        return "deadletter", run.id

    if run.status == "running" and run.execution_token:
        ob = (
            db.query(OutboxJob)
            .filter(
                OutboxJob.claim_token == run.execution_token,
                OutboxJob.status == "running",
            )
            .first()
        )
        if ob is not None and _lease_active(ob.lease_expires_at, now):
            return "busy", run.id
        was_running = True
    else:
        was_running = False

    run.status = "running"
    run.execution_token = claim_token
    run.claim_count = (run.claim_count or 0) + 1
    run.started_at = now
    if was_running:
        run.completed_at = None
        run.error_message = None
    db.flush()
    return ("acquired" if not was_running else "takeover"), run.id


def handle_memory_maintenance(claimed: ClaimedJob) -> HandlerResult:
    """Phase 4 长期记忆生命周期维护 Handler（三阶段 + CONTINUE 分页）。

    - Phase A：claim 校验 + 解析/接管 Run + 选/建 Batch 并冻结 Input。
    - Phase B1：纯函数 plan_batch（无 DB、无 LLM）。
    - Phase B2：双重 fencing + input_hash 比对 + 幂等持久化 Action。
    - Phase C：消费已持久化 Action，原子 finalize + 推进游标；
      若本 lane 仍有未处理 seed 或存在其他 lane 剩余候选 → CONTINUE，
      全部 lane 穷尽 → Run succeeded → COMPLETED（OutboxJob 由 Worker finalize）。
    """
    from aiive.memory.memory_maintenance_planner import plan_batch
    from aiive.memory.memory_store import MemoryStore
    from aiive.memory.recall_config import MaintenanceConfig

    cfg = MaintenanceConfig()
    now = datetime.now(timezone.utc)

    # ════════════ Phase A ════════════
    db_a = SessionLocal()
    try:
        outbox_a = db_a.query(OutboxJob).filter(
            OutboxJob.id == claimed.id
        ).with_for_update().first()
        if outbox_a is None:
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "outbox_not_found")
        if not _claim_matches(outbox_a, claimed, now):
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "claim_expired_or_taken")

        decision, run_id = _resolve_maintenance_run(
            db_a, claimed.id, claimed.payload.get("operation_id", ""),
            claimed.claim_token, now,
        )
        if decision == "already_succeeded":
            db_a.rollback()
            return HandlerResult(HandlerOutcome.COMPLETED, "already_succeeded")
        if decision == "deadletter":
            db_a.rollback()
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "run_deadletter",
                                 terminal_reason="maintenance_run_deadletter")
        if decision == "busy":
            db_a.rollback()
            return HandlerResult(HandlerOutcome.RETRY_LATER, "run_busy")

        m_run = db_a.get(MemoryMaintenanceRun, run_id)
        if m_run is None:
            db_a.rollback()
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "run_not_found", terminal_reason="run_not_found")
        batch = _ensure_batch_and_freeze(db_a, m_run, cfg, now)
        if batch is None:
            # _ensure 已标记 Run succeeded（无剩余候选）
            db_a.commit()
            return HandlerResult(HandlerOutcome.COMPLETED, "run_succeeded_no_work")
        db_a.commit()
        batch_id = batch.id
        execution_token = claimed.claim_token
    except NonRetryableJobError as e:
        db_a.rollback()
        return HandlerResult(HandlerOutcome.NON_RETRYABLE, str(e),
                             terminal_reason="phase_a")
    except Exception as e:
        db_a.rollback()
        logger.exception("memory_maintenance Phase A failed")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"Phase A failed: {e}")
    finally:
        db_a.close()

    # ════════════ Phase B1（纯函数，无 DB）+ B2 + C ════════════
    db_b = SessionLocal()
    try:
        outbox_b = db_b.query(OutboxJob).filter(
            OutboxJob.id == claimed.id
        ).with_for_update().first()
        if outbox_b is None or not _claim_matches(outbox_b, claimed, datetime.now(timezone.utc)):
            db_b.rollback()
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "claim_lost_phase_bc")

        run_b = db_b.query(MemoryMaintenanceRun).filter(
            MemoryMaintenanceRun.id == run_id
        ).with_for_update().first()
        if run_b is None or run_b.execution_token != execution_token:
            db_b.rollback()
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "run_token_mismatch")
        if run_b.status != "running":
            db_b.rollback()
            if run_b.status == "succeeded":
                return HandlerResult(HandlerOutcome.COMPLETED, "already_succeeded")
            return HandlerResult(HandlerOutcome.CLAIM_LOST, f"run_not_running:{run_b.status}")

        batch_b = db_b.query(MemoryMaintenanceBatch).filter(
            MemoryMaintenanceBatch.id == batch_id
        ).with_for_update().first()
        if batch_b is None:
            db_b.rollback()
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "batch_not_found")

        # B1：从已冻结 Input 还原并计算确定性 plan
        input_rows = (
            db_b.query(MemoryMaintenanceInput)
            .filter(MemoryMaintenanceInput.batch_id == batch_id)
            .order_by(MemoryMaintenanceInput.input_sequence.asc())
            .all()
        )
        frozen = [_frozen_input_from_row(mi) for mi in input_rows]
        actions, input_hash, plan_hash = plan_batch(batch_b.id, frozen, cfg, now)

        # B2：input_hash 漂移校验 + 幂等持久化
        if batch_b.input_hash and batch_b.input_hash != input_hash:
            raise NonRetryableJobError(
                f"input_hash 漂移: stored={batch_b.input_hash} recomputed={input_hash}"
            )
        _persist_actions(db_b, batch_b, actions, input_hash, plan_hash, cfg, now)

        # C：执行 + finalize
        _execute_batch(db_b, batch_b, run_b, cfg, now, trace_id=run_id)

        # 判定 CONTINUE / COMPLETED
        store = MemoryStore(db_b)
        any_more = any(
            _maintenance_lane_has_more(db_b, store, run_b, lane, cfg, now)
            for lane in _MAINTENANCE_LANE_ORDER
        )
        if any_more:
            db_b.commit()
            return HandlerResult(HandlerOutcome.CONTINUE, "lane_continue")

        run_b.status = "succeeded"
        run_b.completed_at = now
        db_b.commit()
        return HandlerResult(HandlerOutcome.COMPLETED, "maintenance_run_succeeded")
    except NonRetryableJobError as e:
        db_b.rollback()
        _mark_maintenance_run_failed(run_id, execution_token, str(e))
        return HandlerResult(HandlerOutcome.NON_RETRYABLE, str(e),
                             terminal_reason="phase_bc_non_retryable")
    except Exception as e:
        db_b.rollback()
        _mark_maintenance_run_failed(run_id, execution_token, f"Phase B/C failed: {e}")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"Phase B/C failed: {e}")
    finally:
        db_b.close()


def _mark_maintenance_run_failed(run_id: str, execution_token: str, error: str) -> None:
    """独立短事务：清理 running MemoryMaintenanceRun + 其未完成 Batch。"""
    db_m = SessionLocal()
    try:
        affected = db_m.query(MemoryMaintenanceRun).filter(
            MemoryMaintenanceRun.id == run_id,
            MemoryMaintenanceRun.execution_token == execution_token,
            MemoryMaintenanceRun.status == "running",
        ).update({
            MemoryMaintenanceRun.status: "failed",
            MemoryMaintenanceRun.execution_token: None,
            MemoryMaintenanceRun.error_message: error[:500],
            MemoryMaintenanceRun.completed_at: datetime.now(timezone.utc),
        }, synchronize_session=False)
        if affected:
            db_m.query(MemoryMaintenanceBatch).filter(
                MemoryMaintenanceBatch.run_id == run_id,
                MemoryMaintenanceBatch.status.in_(["frozen", "planned", "applying"]),
            ).update({
                MemoryMaintenanceBatch.status: "deadletter",
                MemoryMaintenanceBatch.completed_at: datetime.now(timezone.utc),
            }, synchronize_session=False)
        db_m.commit()
        if affected == 0:
            logger.warning("_mark_maintenance_run_failed: 无匹配行 run_id=%s", run_id)
    except Exception:
        logger.exception("_mark_maintenance_run_failed 自身失败")
    finally:
        db_m.close()


def register_all(registry: HandlerRegistry) -> None:
    from aiive.tools.operation_executor import handle_tool_operation

    registry.register("tool_operation", handle_tool_operation,
                       supported_schema_versions=frozenset({1}))
    registry.register("memory_extraction", handle_memory_extraction,
                       supported_schema_versions=frozenset({1}))
    registry.register("reminder_delivery", handle_reminder_delivery,
                       supported_schema_versions=frozenset({1}))
    registry.register("core_memory_refresh", handle_core_memory_refresh,
                       supported_schema_versions=frozenset({1}))
    registry.register("memory_vector_refresh", handle_memory_vector_refresh,
                       supported_schema_versions=frozenset({1}))
    registry.register("memory_markdown_project", handle_memory_markdown_project,
                       supported_schema_versions=frozenset({1}))
    # Phase 3
    registry.register("segment_sealing", handle_segment_sealing,
                       supported_schema_versions=frozenset({1}))
    registry.register("epoch_rollover", handle_epoch_rollover,
                       supported_schema_versions=frozenset({1}))
    registry.register("epoch_checkpoint", handle_epoch_checkpoint,
                       supported_schema_versions=frozenset({1}))
    # Phase 4
    registry.register("memory_maintenance", handle_memory_maintenance,
                       supported_schema_versions=frozenset({1}))
    # Phase 5: 统一检索索引刷新 / 全量重建
    registry.register("retrieval_index_refresh", handle_retrieval_index_refresh,
                       supported_schema_versions=frozenset({1}))
    registry.register("retrieval_index_rebuild", handle_retrieval_index_rebuild,
                       supported_schema_versions=frozenset({1}))
    # Phase 6A: Forget Saga
    from aiive.worker.handlers_forget import (
        handle_forget_cascade,
        handle_forget_purge,
        handle_forget_rebuild,
        handle_forget_reconcile,
        handle_forget_verify,
    )
    registry.register("forget_cascade", handle_forget_cascade,
                       supported_schema_versions=frozenset({1}))
    registry.register("forget_rebuild_dependencies", handle_forget_rebuild,
                       supported_schema_versions=frozenset({1}))
    registry.register("forget_purge", handle_forget_purge,
                       supported_schema_versions=frozenset({1}))
    registry.register("forget_verify", handle_forget_verify,
                       supported_schema_versions=frozenset({1}))
    registry.register("forget_reconcile", handle_forget_reconcile,
                       supported_schema_versions=frozenset({1}))
    # Phase 6B: Retention Cleanup
    from aiive.worker.handlers_retention import handle_retention_cleanup
    registry.register("retention_cleanup", handle_retention_cleanup,
                       supported_schema_versions=frozenset({1}))


# ============================================================================
# Internal helpers
# ============================================================================


# ============================================================================
# Phase 3: segment_sealing / epoch_rollover / epoch_checkpoint
# ============================================================================


def _insert_conflict_do_nothing(
    db: Session, model: Any, values: dict[str, Any], conflict_columns: list[str],
) -> None:
    """可移植的 upsert：冲突则忽略（PostgreSQL 用 ON CONFLICT DO NOTHING，SQLite 用 try/except）。

    conflict_columns 必须是唯一键（如 operation_id / outbox_job_id）。
    """
    bind = db.get_bind()
    if getattr(bind.dialect, "name", "") == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = pg_insert(model).values(values).on_conflict_do_nothing(
            index_elements=conflict_columns,
        )
        db.execute(stmt)
        return
    obj = model(**values)
    db.add(obj)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()


def _extract_json(text: str) -> dict[str, Any]:
    """从 LLM 文本中提取首个 JSON 对象。"""
    import json as _json
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("LLM 输出中未找到 JSON")
    return _json.loads(text[start:end + 1])


def _reconstruct_source_turns(db: Session, event_manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """根据 event_manifest 重建 source_turns（供 LLM 概要用）。"""
    event_ids = [m["event_id"] for m in event_manifest]
    if not event_ids:
        return []
    events = db.query(Event).filter(Event.id.in_(event_ids)).all()
    event_order = {
        manifest["event_id"]: index
        for index, manifest in enumerate(event_manifest)
    }
    by_turn: dict[str, dict[str, Any]] = {}
    # event_manifest 已在冻结时按 (turn_sequence, turn_event_index) 排序；
    # 不可使用随机 UUID turn_id 排序，否则摘要会打乱跨 Turn 的因果顺序。
    for e in sorted(events, key=lambda item: event_order.get(item.id, len(event_order))):
        turn = by_turn.setdefault(e.turn_id or "", {
            "turn_id": e.turn_id,
            "user_message": "",
            "assistant_reply": "",
            "tool_calls": [],
            "tool_results": [],
        })
        et = e.event_type
        pl = e.payload or {}
        if et == "user_message":
            turn["user_message"] = pl.get("content", "")
        elif et == "llm_response":
            turn["assistant_reply"] = pl.get("content", "")
        elif et == "tool_call":
            turn["tool_calls"].append({"name": pl.get("name"), "params": pl.get("params")})
        elif et == "tool_result":
            turn["tool_results"].append({
                "name": pl.get("name"),
                "status": pl.get("status"),
                "tool_call_id": pl.get("tool_call_id"),
            })
    return list(by_turn.values())


def _verify_event_hashes(db: Session, event_manifest: list[dict[str, Any]]) -> tuple[bool, str | None]:
    """逐条校验 Event content_hash（Item 3 Phase B）。"""
    from aiive.runtime.compaction import event_content_hash
    ids = [m["event_id"] for m in event_manifest]
    if not ids:
        return True, None
    events = {e.id: e for e in db.query(Event).filter(Event.id.in_(ids)).all()}
    for m in event_manifest:
        e = events.get(m["event_id"])
        if e is None:
            return False, m["event_id"]
        if event_content_hash(e) != m["content_hash"]:
            return False, m["event_id"]
    return True, None


def _run_cover_checks(ci: CompactionInput, summary_payload: dict[str, Any]) -> list[str]:
    """确定性覆盖校验（Item H）：boundary snapshot 字段必须被 Summary 保留。"""
    snap: dict[str, Any] = ci.working_state_snapshot or {}
    violations: list[str] = []

    def _descs(items: list[dict[str, Any]] | None) -> set[str]:
        return {i.get("description") or i.get("ref") or "" for i in (items or [])}

    boundary_open = _descs(snap.get("open_loops", []))
    summary_open = _descs(summary_payload.get("open_loops", []))
    if not boundary_open.issubset(summary_open):
        violations.append("open_loops 未完整保留")

    boundary_constraints = _descs(snap.get("active_constraints", []))
    summary_constraints = _descs(summary_payload.get("active_constraints", []))
    if not boundary_constraints.issubset(summary_constraints):
        violations.append("active_constraints 未完整保留")

    boundary_artifacts = {a.get("ref") for a in (snap.get("artifact_refs") or [])}
    summary_artifacts = {a.get("ref") for a in (summary_payload.get("artifacts") or [])}
    if not boundary_artifacts.issubset(summary_artifacts):
        violations.append("artifact_refs 未完整保留")

    boundary_vts = {v.get("tool_call_id") for v in (snap.get("verified_tool_states") or []) if v.get("tool_call_id")}
    summary_vts = {t.get("tool_call_id") for t in (summary_payload.get("important_tool_results") or [])}
    if not boundary_vts.issubset(summary_vts):
        violations.append("verified_tool_states 未完整保留")

    turn_ids_from_manifest = [m["turn_id"] for m in ci.turn_manifest]
    if sorted(turn_ids_from_manifest) != sorted(summary_payload.get("source_turn_ids") or []):
        violations.append("source_turn_ids 与 manifest 不一致")

    return violations


def _resolve_phase3_run(
    db: Session, run_cls: Any, outbox_job_id: str, defaults: dict[str, Any], claim_token: str,
) -> tuple[str, str]:
    """创建/接管 Phase 3 Run（CompactionRun / CheckpointRun）。

    一 OutboxJob 一 Run：以 outbox_job_id 唯一键 upsert（见 Phase_3.md D.5）。
    """
    import uuid as _uuid
    _insert_conflict_do_nothing(
        db, run_cls,
        {"id": str(_uuid.uuid4()), "outbox_job_id": outbox_job_id, **defaults},
        conflict_columns=["outbox_job_id"],
    )
    run = db.query(run_cls).filter(
        run_cls.outbox_job_id == outbox_job_id,
    ).with_for_update().first()
    if run is None:
        raise NonRetryableJobError("无法解析 Phase 3 Run")

    if run.status == "succeeded":
        return "already_succeeded", run.id
    if run.status == "deadletter":
        return "deadletter", run.id

    # execution_token 非空表示已有 Worker 持锁；为空则是本次新建的 Run（默认 running）
    if run.status == "running" and run.execution_token:
        ob = db.query(OutboxJob).filter(
            OutboxJob.claim_token == run.execution_token,
            OutboxJob.status == "running",
        ).first()
        if ob is not None and _lease_active(ob.lease_expires_at, datetime.now(timezone.utc)):
            return "busy", run.id
        was_running = True  # 旧持锁者租约已失效 → 接管
    else:
        was_running = False  # 首次获取

    run.status = "running"
    run.execution_token = claim_token
    run.attempt_count = (run.attempt_count or 0) + 1
    run.started_at = datetime.now(timezone.utc)
    if was_running:
        run.completed_at = None
        run.error_message = None
    db.flush()
    return ("acquired" if not was_running else "takeover"), run.id


def _build_summary_prompt(source_turns: list[dict[str, Any]], tool_items: list[dict[str, Any]], failure_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """构造摘要 Prompt（Item 4：仅局部 item_ref，无真实稳定 ID）。"""
    tool_lines = "\n".join(
        f"  {t['item_ref']}: tool={t.get('tool_name')}, status={t.get('state', {}).get('ok')}"
        for t in tool_items
    ) or "  （无）"
    fail_lines = "\n".join(
        f"  {f['item_ref']}: tool={f.get('tool_name')}, error={f.get('error')}"
        for f in failure_items
    ) or "  （无）"

    system = get_prompt_registry().render(
        "compaction.segment_summary_system",
    ).content
    user = get_prompt_registry().render(
        "compaction.segment_summary_user",
        source_turns=str(source_turns),
        tool_lines=tool_lines,
        fail_lines=fail_lines,
    ).content
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _build_extract_fallback_summary(source_turns: list[dict[str, Any]]) -> dict[str, Any]:
    """最终重试仍失败时生成无推断的抽取式摘要，避免 sealing 永久阻塞。"""
    user_parts = [
        str(turn.get("user_message", "")).strip()
        for turn in source_turns
        if str(turn.get("user_message", "")).strip()
    ]
    assistant_parts = [
        str(turn.get("assistant_reply", "")).strip()
        for turn in source_turns
        if str(turn.get("assistant_reply", "")).strip()
    ]
    return {
        "goal": "\n".join(user_parts)[-4000:],
        "outcome": "\n".join(assistant_parts)[-4000:],
        "decisions": [],
        "entities": [],
        "tool_result_summaries": [],
        "failure_explanations": [],
    }


def handle_segment_sealing(claimed: ClaimedJob) -> HandlerResult:
    """三阶段 Segment 摘要 Handler（Phase 3）。

    Phase A：验证 claim + Segment sealing/ source_hash + 解析 CompactionRun
    Phase B：逐条校验 Event hash + LLM 语义摘要 + item_ref 确定性合并
    Phase C：双重 fencing + 覆盖校验 + 写 Summary/Segment + 事务性 enqueue rollover/checkpoint
    Handler 不 finalize OutboxJob（由 OutboxWorker 负责）。
    """
    from aiive.memory.recall_config import RecallConfig
    from aiive.runtime.compaction import (
        build_item_ref_map,
        count_summary_tokens,
        derive_unresolved_failures,
        merge_summary,
        validate_llm_semantic_output,
    )

    payload = claimed.payload
    segment_id: str = payload.get("segment_id", "")
    compaction_input_id: str = payload.get("compaction_input_id", "")
    source_hash: str = payload.get("source_hash", "")
    summary_version: int = payload.get("summary_version", 1)

    # ── Phase A ──
    db_a = SessionLocal()
    try:
        outbox_a = db_a.query(OutboxJob).filter(OutboxJob.id == claimed.id).with_for_update().first()
        if outbox_a is None:
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "outbox_not_found")
        now = datetime.now(timezone.utc)
        if not _claim_matches(outbox_a, claimed, now):
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "claim_expired_or_taken")

        seg = db_a.query(Segment).filter(Segment.id == segment_id).with_for_update().first()
        if seg is None:
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "segment_not_found",
                                 terminal_reason="segment_not_found")
        if seg.status == "sealed":
            return HandlerResult(HandlerOutcome.COMPLETED, "already_sealed")
        if seg.status != "sealing":
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "segment_not_sealing",
                                 terminal_reason="segment_not_sealing")
        if seg.source_hash != source_hash:
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "source_hash_mismatch",
                                 terminal_reason="source_hash_mismatch")

        decision, run_id = _resolve_phase3_run(
            db_a, CompactionRun, outbox_job_id=claimed.id,
            defaults={
                "segment_id": segment_id,
                "compaction_input_id": compaction_input_id,
                "source_hash": source_hash,
                "summary_version": summary_version,
            },
            claim_token=claimed.claim_token,
        )
        if decision == "already_succeeded":
            db_a.rollback()
            return HandlerResult(HandlerOutcome.COMPLETED, f"already_succeeded run_id={run_id}")
        if decision == "deadletter":
            db_a.rollback()
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "run_deadletter",
                                 terminal_reason="compaction_run_deadletter")
        if decision == "busy":
            db_a.rollback()
            return HandlerResult(HandlerOutcome.RETRY_LATER, "run_busy")
        db_a.commit()
        execution_token = claimed.claim_token
    except NonRetryableJobError as e:
        db_a.rollback()
        return HandlerResult(HandlerOutcome.NON_RETRYABLE, str(e), terminal_reason="phase_a")
    except Exception as e:
        db_a.rollback()
        logger.exception("segment_sealing Phase A failed")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"Phase A failed: {e}")
    finally:
        db_a.close()

    # ── Phase B（事务外，无 DB Session 持有）──
    llm_output: dict[str, Any] | None = None
    summary_model_id = settings.aiive_llm_model
    try:
        db_b = SessionLocal()
        try:
            ci = db_b.query(CompactionInput).filter(CompactionInput.id == compaction_input_id).first()
            if ci is None:
                raise NonRetryableJobError("CompactionInput 不存在")

            ok, bad = _verify_event_hashes(db_b, ci.event_manifest)
            if not ok:
                raise NonRetryableJobError(f"Event content_hash 不一致: {bad}")

            source_turns = _reconstruct_source_turns(db_b, ci.event_manifest)
            ws = ci.working_state_snapshot or {}
            verified = ws.get("verified_tool_states", [])
            failures = derive_unresolved_failures(db_b, ci.segment_id)
            item_ref_map = build_item_ref_map(verified, failures)
            valid_refs = set(item_ref_map["ref_to_tool_call_id"].keys())
        finally:
            db_b.close()

        llm = _get_llm_client()
        messages = _build_summary_prompt(
            source_turns, item_ref_map["tool_items"], item_ref_map["failure_items"],
        )
        warnings: list[str] = []
        try:
            for attempt in range(2):
                resp = llm.chat(
                    messages,
                    model=settings.aiive_llm_model,
                    temperature=0,
                    json_mode=True,
                )
                raw = getattr(resp, "content", None) or getattr(resp, "text", "") or str(resp)
                try:
                    if getattr(resp, "finish_reason", "") == "length":
                        raise ValueError("JSON 输出被截断")
                    llm_output = _extract_json(raw)
                    warnings = validate_llm_semantic_output(llm_output, valid_refs)
                    break
                except (ValueError, TypeError, KeyError) as error:
                    if attempt == 1:
                        raise
                    messages = [
                        *messages,
                        {"role": "assistant", "content": raw},
                        {
                            "role": "user",
                            "content": get_prompt_registry().render(
                                "compaction.segment_summary_retry",
                                error_text=str(error)[:500],
                            ).content,
                        },
                    ]
        except Exception:
            if claimed.retry_count + 1 < claimed.max_retries:
                raise
            logger.exception(
                "segment_sealing 最终重试仍无法生成结构化摘要，使用抽取式降级: "
                + "segment_id=%s",
                segment_id,
            )
            llm_output = _build_extract_fallback_summary(source_turns)
            summary_model_id = "deterministic-extractive-fallback"
        if llm_output is None:
            raise ValueError("摘要模型未返回可用 JSON")
        for w in warnings:
            logger.warning("segment_sealing: %s", w)
    except NonRetryableJobError as e:
        _mark_phase3_run_failed(CompactionRun, run_id, execution_token, str(e))
        return HandlerResult(HandlerOutcome.NON_RETRYABLE, str(e),
                             terminal_reason="phase_b_non_retryable")
    except Exception as e:
        _mark_phase3_run_failed(CompactionRun, run_id, execution_token, f"Phase B failed: {e}")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"Phase B failed: {e}")

    # ── Phase C ──
    db_c = SessionLocal()
    try:
        outbox_c = db_c.query(OutboxJob).filter(OutboxJob.id == claimed.id).with_for_update().first()
        if outbox_c is None or not _claim_matches(outbox_c, claimed, datetime.now(timezone.utc)):
            db_c.rollback()
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "claim_lost_in_phase_c")

        run_c = db_c.query(CompactionRun).filter(CompactionRun.id == run_id).with_for_update().first()
        if run_c is None or run_c.execution_token != execution_token:
            db_c.rollback()
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "run_token_mismatch")
        if run_c.status != "running":
            db_c.rollback()
            if run_c.status == "succeeded":
                return HandlerResult(HandlerOutcome.COMPLETED, "already_succeeded")
            return HandlerResult(HandlerOutcome.CLAIM_LOST, f"run_not_running:{run_c.status}")

        ci_c = db_c.query(CompactionInput).filter(CompactionInput.id == compaction_input_id).first()
        if ci_c is None:
            db_c.rollback()
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "ci_not_found", terminal_reason="ci_not_found")

        seg_c = db_c.query(Segment).filter(Segment.id == segment_id).with_for_update().first()
        if seg_c is None or seg_c.status != "sealing":
            db_c.rollback()
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "segment_not_sealing_phase_c")

        ws_snap = ci_c.working_state_snapshot or {}
        verified = ws_snap.get("verified_tool_states", [])
        failures = derive_unresolved_failures(db_c, ci_c.segment_id)
        item_ref_map = build_item_ref_map(verified, failures)

        source_turn_ids = [m["turn_id"] for m in ci_c.turn_manifest]
        source_event_ids = [m["event_id"] for m in ci_c.event_manifest]
        summary_payload = merge_summary(
            llm_output,
            boundary_snapshot=ws_snap,
            verified_tool_states=verified,
            unresolved_failures=failures,
            item_ref_map=item_ref_map,
            source_turn_ids=source_turn_ids,
            source_event_ids=source_event_ids,
            source_hash=source_hash,
            summary_version=summary_version,
            model_id=summary_model_id,
            token_count=0,
        )
        summary_payload["token_count"] = count_summary_tokens(
            settings.aiive_llm_model,
            _json.dumps(summary_payload, ensure_ascii=False, default=str),
        )

        violations = _run_cover_checks(ci_c, summary_payload)
        if violations:
            raise NonRetryableJobError(f"覆盖校验失败: {violations}")

        summary = SegmentSummary(segment_id=segment_id, **summary_payload)
        db_c.add(summary)
        db_c.flush()

        seg_c.summary_id = summary.id
        seg_c.status = "sealed"
        seg_c.sealed_at = datetime.now(timezone.utc)
        run_c.status = "succeeded"
        run_c.completed_at = datetime.now(timezone.utc)

        # 条件性 enqueue（同一事务，只创建 Job，不执行 rollover/checkpoint 本体）
        _enqueue_epoch_jobs(db_c, seg_c, RecallConfig())

        # Phase 5: 刷新 segment_summary 进统一检索索引（同一事务）
        # op_id 含 source_version，与文档 O 节格式及 memory 路径一致（每版本独立 job）。
        _insert_job_do_nothing(
            db_c, job_type="retrieval_index_refresh",
            operation_id=f"retrieval_refresh:segment_summary:{summary.id}:{summary.summary_version}",
            extra_payload={
                "source_type": "segment_summary",
                "source_id": summary.id,
                "source_version": summary.summary_version,
                "event_type": "segment.sealed",
            },
        )

        db_c.commit()
        return HandlerResult(HandlerOutcome.COMPLETED, "segment_sealed")
    except NonRetryableJobError as e:
        db_c.rollback()
        _mark_phase3_run_failed(CompactionRun, run_id, execution_token, str(e))
        return HandlerResult(HandlerOutcome.NON_RETRYABLE, str(e),
                             terminal_reason="cover_check_failed")
    except Exception as e:
        db_c.rollback()
        _mark_phase3_run_failed(CompactionRun, run_id, execution_token, f"Phase C failed: {e}")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"Phase C failed: {e}")
    finally:
        db_c.close()


def _enqueue_epoch_jobs(db: Session, sealed_segment: Segment, cfg: Any) -> None:
    """Phase C：条件性事务 enqueue epoch_rollover / epoch_checkpoint（Item 4）。"""
    epoch = db.query(Epoch).filter(Epoch.id == sealed_segment.epoch_id).first()
    if epoch is None:
        return
    sealed_count = db.query(Segment).filter(
        Segment.epoch_id == epoch.id, Segment.status == "sealed",
    ).count()

    if epoch.status == "active" and sealed_count >= cfg.max_segments_per_epoch:
        _insert_job_do_nothing(
            db, job_type="epoch_rollover",
            operation_id=f"epoch_rollover:{epoch.id}",
            extra_payload={"thread_id": sealed_segment.thread_id},
        )

    if epoch.status == "sealing":
        non_sealed = db.query(Segment).filter(
            Segment.epoch_id == epoch.id,
            Segment.status != "sealed",
        ).count()
        total = db.query(Segment).filter(Segment.epoch_id == epoch.id).count()
        if total > 0 and non_sealed == 0:
            eci = (
                db.query(EpochCompactionInput)
                .filter(EpochCompactionInput.epoch_id == epoch.id)
                .order_by(EpochCompactionInput.created_at.desc())
                .first()
            )
            if eci is not None:
                _insert_job_do_nothing(
                    db, job_type="epoch_checkpoint",
                    operation_id=f"epoch_checkpoint:{epoch.id}",
                    extra_payload={
                        "epoch_id": epoch.id,
                        "epoch_compaction_input_id": eci.id,
                        "boundary_hash": eci.snapshot_hash,
                        "checkpoint_version": eci.checkpoint_version,
                    },
                )


def _insert_job_do_nothing(
    db: Session, job_type: str, operation_id: str, extra_payload: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"schema_version": 1}
    if extra_payload:
        payload.update(extra_payload)
    _insert_conflict_do_nothing(
        db, OutboxJob,
        {
            "operation_id": operation_id,
            "job_type": job_type,
            "status": "pending",
            "payload": payload,
            "max_retries": 3,
        },
        conflict_columns=["operation_id"],
    )


def _mark_phase3_run_failed(run_cls: Any, run_id: str, execution_token: str, error: str) -> None:
    """独立短事务：清理 running Phase 3 Run。"""
    db_m = SessionLocal()
    try:
        affected = db_m.query(run_cls).filter(
            run_cls.id == run_id,
            run_cls.execution_token == execution_token,
            run_cls.status == "running",
        ).update({
            run_cls.status: "failed",
            run_cls.execution_token: None,
            run_cls.error_message: error[:500],
            run_cls.completed_at: datetime.now(timezone.utc),
        }, synchronize_session=False)
        db_m.commit()
        if affected == 0:
            logger.warning("_mark_phase3_run_failed: 无匹配行 run_id=%s", run_id)
    except Exception:
        logger.exception("_mark_phase3_run_failed 自身失败")
    finally:
        db_m.close()


def handle_epoch_rollover(claimed: ClaimedJob) -> HandlerResult:
    """Epoch rollover Handler（Item 5）：短事务调用 begin_epoch_sealing，幂等。"""
    from aiive.runtime.epoch_manager import EpochManager

    payload = claimed.payload
    thread_id: str = payload.get("thread_id", "")

    db = SessionLocal()
    try:
        outbox = db.query(OutboxJob).filter(OutboxJob.id == claimed.id).with_for_update().first()
        if outbox is None:
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "outbox_not_found")
        now = datetime.now(timezone.utc)
        if not _claim_matches(outbox, claimed, now):
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "claim_expired_or_taken")

        epoch = db.query(Epoch).filter(
            Epoch.thread_id == thread_id, Epoch.status == "active",
        ).with_for_update().first()
        if epoch is None:
            db.commit()
            return HandlerResult(HandlerOutcome.COMPLETED, "already_succeeded_no_active_epoch")

        mgr = EpochManager()
        result = mgr.begin_epoch_sealing(db, thread_id)
        if result.stale:
            db.commit()
            return HandlerResult(HandlerOutcome.COMPLETED, f"already_succeeded {result.reason}")
        db.commit()
        return HandlerResult(HandlerOutcome.COMPLETED, f"epoch_rolled_over new_epoch_id={result.new_epoch_id}")
    except Exception as e:
        db.rollback()
        logger.exception("epoch_rollover failed")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"epoch_rollover failed: {e}")
    finally:
        db.close()


def _build_epoch_checkpoint_payload(
    epoch_input: EpochCompactionInput,
    summaries: list[SegmentSummary],
    checkpoint_version: int,
) -> dict[str, Any]:
    """从有序 Segment 摘要确定性聚合 Epoch checkpoint。

    checkpoint 不是再次生成“摘要的摘要”；它复制最近的语义事实和边界
    WorkingState，避免多层 LLM 压缩导致漂移。
    """
    def _recent_unique(
        attr: str, identity_key: str, limit: int,
    ) -> list[Any]:
        values: list[Any] = []
        seen: set[str] = set()
        for summary in reversed(summaries):
            for item in reversed(getattr(summary, attr, None) or []):
                identity = (
                    str(item.get(identity_key, ""))
                    if isinstance(item, dict)
                    else str(item)
                )
                if not identity or identity in seen:
                    continue
                seen.add(identity)
                values.append(item)
                if len(values) >= limit:
                    return list(reversed(values))
        return list(reversed(values))

    milestones = [
        {"description": summary.outcome, "segment_id": summary.segment_id}
        for summary in summaries
        if summary.outcome
    ][-12:]
    fallback_goal = next(
        (summary.goal for summary in reversed(summaries) if summary.goal),
        None,
    )
    source_hashes = [summary.source_hash for summary in summaries if summary.source_hash]
    return {
        "current_goal": epoch_input.current_objective or fallback_goal,
        "completed_milestones": milestones,
        "open_loops": epoch_input.open_loops,
        "active_constraints": epoch_input.active_constraints,
        "current_decisions": _recent_unique("decisions", "what", 24),
        "referenced_artifacts": epoch_input.artifact_refs,
        "relevant_entities": _recent_unique("entities", "name", 24),
        "latest_verified_tool_states": epoch_input.verified_tool_states,
        "source_segment_ids": epoch_input.source_segment_ids,
        "source_hashes": source_hashes or epoch_input.source_hashes,
        "version": checkpoint_version,
        "token_count": 0,
    }


def handle_epoch_checkpoint(claimed: ClaimedJob) -> HandlerResult:
    """EpochCheckpoint Handler：从 EpochCompactionInput + SegmentSummary 聚合生成 Checkpoint。"""
    payload = claimed.payload
    epoch_id: str = payload.get("epoch_id", "")
    epoch_compaction_input_id: str = payload.get("epoch_compaction_input_id", "")
    boundary_hash: str = payload.get("boundary_hash", "")
    checkpoint_version: int = payload.get("checkpoint_version", 1)

    db_a = SessionLocal()
    try:
        outbox_a = db_a.query(OutboxJob).filter(OutboxJob.id == claimed.id).with_for_update().first()
        if outbox_a is None:
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "outbox_not_found")
        now = datetime.now(timezone.utc)
        if not _claim_matches(outbox_a, claimed, now):
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "claim_expired_or_taken")

        eci = db_a.query(EpochCompactionInput).filter(
            EpochCompactionInput.id == epoch_compaction_input_id,
        ).with_for_update().first()
        if eci is None:
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "eci_not_found",
                                 terminal_reason="eci_not_found")

        decision, run_id = _resolve_phase3_run(
            db_a, CheckpointRun, outbox_job_id=claimed.id,
            defaults={
                "epoch_id": epoch_id,
                "epoch_compaction_input_id": epoch_compaction_input_id,
                "boundary_hash": boundary_hash,
                "checkpoint_version": checkpoint_version,
            },
            claim_token=claimed.claim_token,
        )
        if decision == "already_succeeded":
            db_a.rollback()
            return HandlerResult(HandlerOutcome.COMPLETED, "already_succeeded")
        if decision == "deadletter":
            db_a.rollback()
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "run_deadletter",
                                 terminal_reason="checkpoint_run_deadletter")
        if decision == "busy":
            db_a.rollback()
            return HandlerResult(HandlerOutcome.RETRY_LATER, "run_busy")
        db_a.commit()
        execution_token = claimed.claim_token
    except NonRetryableJobError as e:
        db_a.rollback()
        return HandlerResult(HandlerOutcome.NON_RETRYABLE, str(e), terminal_reason="phase_a")
    except Exception as e:
        db_a.rollback()
        logger.exception("epoch_checkpoint Phase A failed")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"Phase A failed: {e}")
    finally:
        db_a.close()

    # Phase B：确定性聚合（无 LLM）
    db_b = SessionLocal()
    try:
        eci = db_b.query(EpochCompactionInput).filter(
            EpochCompactionInput.id == epoch_compaction_input_id,
        ).first()
        if eci is None:
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "eci_not_found", terminal_reason="eci_not_found")
        summaries = (
            db_b.query(SegmentSummary)
            .join(Segment, Segment.id == SegmentSummary.segment_id)
            .filter(Segment.epoch_id == eci.epoch_id)
            .order_by(Segment.segment_no)
            .all()
        )
        checkpoint_payload = _build_epoch_checkpoint_payload(
            eci, summaries, checkpoint_version,
        )
        from aiive.runtime.compaction import count_summary_tokens
        checkpoint_payload["token_count"] = count_summary_tokens(
            settings.aiive_llm_model,
            _json.dumps(checkpoint_payload, ensure_ascii=False, default=str),
        )
    finally:
        db_b.close()

    # Phase C
    db_c = SessionLocal()
    try:
        outbox_c = db_c.query(OutboxJob).filter(OutboxJob.id == claimed.id).with_for_update().first()
        if outbox_c is None or not _claim_matches(outbox_c, claimed, datetime.now(timezone.utc)):
            db_c.rollback()
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "claim_lost_phase_c")

        run_c = db_c.query(CheckpointRun).filter(CheckpointRun.id == run_id).with_for_update().first()
        if run_c is None or run_c.execution_token != execution_token:
            db_c.rollback()
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "run_token_mismatch")

        epoch = db_c.query(Epoch).filter(Epoch.id == epoch_id).with_for_update().first()
        if epoch is None:
            db_c.rollback()
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "epoch_not_found", terminal_reason="epoch_not_found")
        checkpoint = EpochCheckpoint(epoch_id=epoch_id, **checkpoint_payload)
        db_c.add(checkpoint)
        db_c.flush()
        epoch.checkpoint_id = checkpoint.id
        epoch.status = "sealed"
        epoch.sealed_at = datetime.now(timezone.utc)
        run_c.status = "succeeded"

        # Phase 5: 刷新 epoch_checkpoint 进统一检索索引（同一事务）
        # op_id 含 source_version，与文档 O 节格式及 memory 路径一致（每版本独立 job）。
        _insert_job_do_nothing(
            db_c, job_type="retrieval_index_refresh",
            operation_id=f"retrieval_refresh:epoch_checkpoint:{checkpoint.id}:{checkpoint.version}",
            extra_payload={
                "source_type": "epoch_checkpoint",
                "source_id": checkpoint.id,
                "source_version": checkpoint.version,
                "event_type": "epoch.checkpointed",
            },
        )
        run_c.completed_at = datetime.now(timezone.utc)
        db_c.commit()
        return HandlerResult(HandlerOutcome.COMPLETED, "epoch_checkpointed")
    except Exception as e:
        db_c.rollback()
        _mark_phase3_run_failed(CheckpointRun, run_id, execution_token, f"Phase C failed: {e}")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"Phase C failed: {e}")
    finally:
        db_c.close()


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

    # 可移植 upsert：PostgreSQL 走 ON CONFLICT DO NOTHING，SQLite 走 try/except
    # （此前无条件使用 postgresql 方言的 pg_insert，SQLite 下直接崩溃）。
    _insert_conflict_do_nothing(
        db, MemoryIngestionRun,
        {
            "id": str(__import__("uuid").uuid4()),
            "source_turn_record_id": source_turn_record_id,
            "extractor_name": "UnifiedMemoryExtractor",
            "extractor_version": "1.0",
            "status": "pending",
        },
        conflict_columns=["source_turn_record_id", "extractor_name", "extractor_version"],
    )

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

        if outbox_job is not None and _lease_active(
            outbox_job.lease_expires_at, datetime.now(timezone.utc),
        ):
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
