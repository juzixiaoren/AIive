"""Deliver persistent task terminal events through the normal Main Agent Turn lifecycle."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from aiive.db.base import SessionLocal
from aiive.db.models import OutboxJob, Thread, TurnEventInbox
from aiive.forget.concurrency import acquire_forget_shared_lock
from aiive.runtime.turn_events import (
    MAX_INJECT_PER_CALL,
    STATUS_EXPIRED,
    STATUS_HANDLED,
    active_turn_for_thread,
    expire_orphaned_task_events,
    stage_task_terminal_event,
)
from aiive.runtime.turn_execution import TurnExecutionService
from aiive.task_runtime.read_repository import TaskReadRepository
from aiive.task_runtime.schemas import TaskStatus
from aiive.worker.job_source_guard import guard_job_sources_visible
from aiive.worker.job_types import MAIN_AGENT_TASK_TERMINAL
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome, HandlerResult
from aiive.worker.outbox_execution_auth import get_session_factory, shared_execution_authorization, verify_claim

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({TaskStatus.SUCCEEDED, TaskStatus.PARTIAL, TaskStatus.FAILED})
WAKE_MESSAGE = "[runtime event] main_agent_task_terminal"


def _wait(reason: str) -> HandlerResult:
    return HandlerResult(HandlerOutcome.RETRY_LATER, reason, datetime.now(timezone.utc) + timedelta(seconds=3))


def _valid_payload(db: Session, thread_id: str, key: str, payload: dict[str, Any]) -> bool:
    task_id, status = payload.get("task_id"), payload.get("status")
    if not isinstance(task_id, str) or not isinstance(status, str) or status not in TERMINAL_STATUSES:
        return False
    if key != f"{MAIN_AGENT_TASK_TERMINAL}:{task_id}:{status}":
        return False
    task = TaskReadRepository(db).get_task(task_id)
    return task is not None and task.thread_id == thread_id and task.status == status


def _stage_due_peers(db: Session, thread_id: str, job_id: str) -> None:
    # A bounded batch of due jobs from this thread can share the same model Turn.
    # Previously attempted events are rearmed by their own claims/backoff.
    now = datetime.now(timezone.utc)
    peers = db.query(OutboxJob.operation_id, OutboxJob.payload).outerjoin(
        TurnEventInbox, TurnEventInbox.dedupe_key == OutboxJob.operation_id,
    ).filter(
        OutboxJob.job_type == MAIN_AGENT_TASK_TERMINAL,
        OutboxJob.schema_version == 1,
        OutboxJob.payload["thread_id"].as_string() == thread_id,
        OutboxJob.id != job_id,
        OutboxJob.status.in_(("pending", "running")),
        or_(OutboxJob.available_at.is_(None), OutboxJob.available_at <= now),
        or_(TurnEventInbox.id.is_(None),
            (TurnEventInbox.status == STATUS_EXPIRED) & (TurnEventInbox.decision == "turn_interrupted")),
    ).order_by(OutboxJob.created_at, OutboxJob.id).limit(MAX_INJECT_PER_CALL - 1).all()
    for key, payload in peers:
        if _valid_payload(db, thread_id, key, payload):
            stage_task_terminal_event(db, thread_id, key, payload)


def handle_main_agent_task_terminal(claimed: ClaimedJob) -> HandlerResult:
    # Use the Outbox claim/source fence for staging only. The Turn then owns its
    # execution lease and checks Forget again at injection, tool read and finalization.
    with shared_execution_authorization():
        factory = get_session_factory()
        fresh, blocked = guard_job_sources_visible(claimed, factory)
        if blocked is not None:
            return blocked
        assert fresh is not None
        thread_id = fresh.payload.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "invalid_task_terminal_thread")

        db = factory()
        try:
            acquire_forget_shared_lock(db)
            thread = db.query(Thread.id).filter(Thread.id == thread_id).with_for_update().scalar()
            if thread is None:
                return HandlerResult(HandlerOutcome.COMPLETED, "thread_unavailable")
            current, reason = verify_claim(db, fresh)
            if current is None:
                return HandlerResult(HandlerOutcome.CLAIM_LOST, reason)
            key = db.query(OutboxJob.operation_id).filter(OutboxJob.id == fresh.id).scalar()
            if not key:
                return HandlerResult(HandlerOutcome.CLAIM_LOST, "task_terminal_job_missing")
            if not _valid_payload(db, thread_id, key, current.payload):
                return HandlerResult(HandlerOutcome.NON_RETRYABLE, "invalid_task_terminal_source")
            expire_orphaned_task_events(db, thread_id)
            row = stage_task_terminal_event(db, thread_id, key, current.payload)
            if row is not None and row.status == STATUS_HANDLED:
                return HandlerResult(HandlerOutcome.COMPLETED, "task_terminal_handled")
            if row is not None and row.decision == "source_blocked":
                db.commit()
                return HandlerResult(HandlerOutcome.COMPLETED, "task_terminal_source_blocked")
            _stage_due_peers(db, thread_id, fresh.id)
            active = active_turn_for_thread(db, thread_id) is not None
            db.commit()
        finally:
            db.close()

    if active:
        return _wait("task_terminal_turn_active")

    turn_id = str(uuid.uuid4())
    response = TurnExecutionService(message_source="runtime_event", task_terminal_wakeup=True).execute_turn(
        WAKE_MESSAGE, thread_id=thread_id, turn_id=turn_id,
    )
    if response.get("error") == "turn_in_progress":
        return _wait("task_terminal_thread_busy")
    if response.get("error") == "turn_cancelled":
        return _wait("task_terminal_turn_cancelled")
    if response.get("error"):
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, str(response["error"]))
    if response.get("event_id") and response.get("reply"):
        try:
            from aiive.api.ws_manager import ws_manager
            ws_manager.broadcast_to_thread_sync(thread_id, "new_message", {**response, "turn_id": turn_id})
        except Exception:
            logger.exception("任务完成回复实时推送失败（已落库）: turn_id=%s", turn_id)

    db = SessionLocal()
    try:
        row = db.query(TurnEventInbox).filter_by(dedupe_key=key).one_or_none()
        if row is not None:
            if row.status == STATUS_HANDLED or row.decision == "source_blocked":
                return HandlerResult(HandlerOutcome.COMPLETED, "task_terminal_" + (row.decision or row.status))
            if row.turn_id == turn_id:
                return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, row.decision or "task_terminal_unhandled")
        return _wait("task_terminal_waiting_for_inbox_batch")
    finally:
        db.close()
