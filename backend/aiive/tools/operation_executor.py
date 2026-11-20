"""副作用工具的持久化入队、有限等待和 Worker 终态提交。"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, override

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aiive.context.run_context import RunContext, RUN_CTX_OUTBOX_WORKER, require_ctx
from aiive.db.base import SessionLocal
from aiive.tools.base import DatabaseTool, coerce_tool_result, outcome_is_unknown
from aiive.db.models import Event, OutboxJob, ToolOperation, TurnRecord
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome, HandlerResult
from aiive.worker.job_source_guard import guard_job_sources_visible
from aiive.worker.outbox_execution_auth import with_execution_authorization

logger = logging.getLogger(__name__)
TOOL_OPERATION_JOB_TYPE = "tool_operation"
_TERMINAL_STATUSES = frozenset({"committed", "failed", "cancelled"})
_waiters_lock = threading.Lock()
_waiters: dict[str, threading.Event] = {}


def _canonical_json(value: Any) -> str:
    """生成稳定 JSON，用于哈希和可持久化结果。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _json_value(value: Any) -> Any:
    """将 handler 结果规范化为 JSON 可持久化值。"""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _waiter(operation_id: str) -> threading.Event:
    """获取进程内 operation 完成通知对象。"""
    with _waiters_lock:
        return _waiters.setdefault(operation_id, threading.Event())


def _notify(operation_id: str) -> None:
    """唤醒等待者，并同步 WorkingState 与线程 WebSocket 的真实终态。"""
    with _waiters_lock:
        event = _waiters.pop(operation_id, None)
    if event is not None:
        event.set()
    operation = _load_operation(operation_id)
    if operation is None:
        return
    if operation.status in _TERMINAL_STATUSES:
        db = SessionLocal()
        try:
            from aiive.runtime.working_state import WorkingStateService

            working_state = WorkingStateService()
            working_state.remove_uncommitted_side_effect(
                db, operation.thread_id, operation.tool_call_id,
            )
            working_state.update_verified_tool_state(
                db,
                operation.thread_id,
                operation.capability_id,
                {
                    "ok": operation.status == "committed",
                    "tool_call_id": operation.tool_call_id,
                    "operation_id": operation.id,
                },
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("同步工具 operation WorkingState 失败: operation_id=%s", operation_id)
        finally:
            db.close()
    try:
        from aiive.api.ws_manager import ws_manager

        ws_manager.broadcast_to_thread_sync(
            operation.thread_id,
            "tool_operation",
            operation_response(operation),
        )
    except Exception:
        logger.exception("推送工具 operation 终态失败: operation_id=%s", operation_id)


def enqueue_tool_operation(
    capability_id: str,
    params: dict[str, Any],
    descriptor_hash: str,
    effect_mode: str,
    run_context: RunContext,
    tool_call_id: str,
) -> ToolOperation:
    """以稳定幂等键原子创建或复用 ToolOperation 与 OutboxJob。"""
    if not run_context.thread_id or not run_context.turn_record_id or not tool_call_id:
        raise ValueError("副作用工具需要 thread_id、turn_record_id 和 tool_call_id")

    canonical_params = _canonical_json(params)
    params_hash = hashlib.sha256(canonical_params.encode("utf-8")).hexdigest()
    key_source = "|".join((run_context.turn_record_id, tool_call_id, capability_id, params_hash))
    idempotency_key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()

    db = SessionLocal()
    try:
        turn = db.query(TurnRecord).filter(TurnRecord.id == run_context.turn_record_id).with_for_update().one_or_none()
        if turn is not None and turn.cancel_requested_at is not None:
            from aiive.tools.base import ToolCancelled
            raise ToolCancelled()
        existing = db.query(ToolOperation).filter(
            ToolOperation.idempotency_key == idempotency_key,
        ).first()
        if existing is not None:
            db.expunge(existing)
            return existing

        operation_id = str(uuid.uuid4())
        job = OutboxJob(
            operation_id=f"tool_operation:{idempotency_key}",
            job_type=TOOL_OPERATION_JOB_TYPE,
            status="pending",
            payload={"tool_operation_id": operation_id},
            trace_id=run_context.trace_id,
            max_retries=3,
            schema_version=1,
        )
        db.add(job)
        db.flush()
        operation = ToolOperation(
            id=operation_id,
            idempotency_key=idempotency_key,
            thread_id=run_context.thread_id,
            turn_record_id=run_context.turn_record_id,
            turn_id=run_context.turn_id,
            tool_call_id=tool_call_id,
            capability_id=capability_id,
            params=_json_value(params),
            params_hash=params_hash,
            descriptor_hash=descriptor_hash,
            status="queued",
            effect_mode=effect_mode,
            outbox_job_id=job.id,
            trace_id=run_context.trace_id,
        )
        db.add(operation)
        db.commit()
        db.refresh(operation)
        db.expunge(operation)
        return operation
    except IntegrityError:
        db.rollback()
        existing = db.query(ToolOperation).filter(
            ToolOperation.idempotency_key == idempotency_key,
        ).one()
        db.expunge(existing)
        return existing
    finally:
        db.close()


def wait_for_tool_operation(operation_id: str, timeout_seconds: float) -> dict[str, Any]:
    """有限等待持久化终态；超时只报告未知，不取消或重复执行。"""
    event = _waiter(operation_id)
    try:
        operation = _load_operation(operation_id)
        if operation is not None and operation.status not in _TERMINAL_STATUSES:
            deadline = time.monotonic() + max(0.0, timeout_seconds)
            while operation is not None and operation.status not in _TERMINAL_STATUSES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                event.wait(timeout=min(0.1, remaining))
                operation = _load_operation(operation_id)
    finally:
        # 防止 _waiters 泄漏：无论 _notify 是否发生，等待结束后移除自己创建的 waiter
        with _waiters_lock:
            if _waiters.get(operation_id) is event:
                _waiters.pop(operation_id, None)
    if operation is None:
        return {
            "ok": False,
            "error_type": "execution_unknown",
            "execution_status": "execution_unknown",
            "operation_id": operation_id,
            "error": "工具操作状态无法读取，禁止重复执行",
        }
    return operation_response(operation)


def _load_operation(operation_id: str) -> ToolOperation | None:
    """使用短会话读取 operation 当前状态。"""
    db = SessionLocal()
    try:
        operation = db.get(ToolOperation, operation_id)
        if operation is not None:
            db.expunge(operation)
        return operation
    finally:
        db.close()


def request_turn_cancellation(db: Session, turn_record_id: str, now: datetime) -> None:
    """Caller holds the Turn lock; preserve committed receipts, stop queued work."""
    operations = db.query(ToolOperation).filter(
        ToolOperation.turn_record_id == turn_record_id,
        ToolOperation.status.in_(["queued", "running"]),
    ).with_for_update(skip_locked=True).all()
    for operation in operations:
        operation.cancel_requested_at = operation.cancel_requested_at or now
        if operation.status == "queued":
            operation.status = "cancelled"
            operation.completed_at = now
            operation.terminal_reason = "cancelled_before_start"
            db.add(_build_terminal_event(operation, "cancelled"))


def operation_response(operation: ToolOperation) -> dict[str, Any]:
    """将持久化状态转换为统一 Registry 响应。

    outcome_known / side_effect_started 优先取持久化回执，避免失败回执丢失
    "已经产生了部分外部影响"这一事实。
    """
    common = {
        "operation_id": operation.id,
        "tool_call_id": operation.tool_call_id,
        "execution_status": operation.status,
    }
    receipt = operation.effect_receipt if isinstance(operation.effect_receipt, dict) else {}
    if operation.status == "committed":
        return {
            "ok": True,
            "result": operation.result_payload,
            "outcome_known": True,
            "side_effect_started": bool(receipt.get("side_effect_started", True)),
            **common,
        }
    if operation.status in {"failed", "cancelled"}:
        details = {}
        if operation.status == "failed" and operation.capability_id == "remember_or_update":
            from aiive.memory.memory_types import WriteOutcome
            from aiive.memory.memory_write_service import WriteResult

            details = {"details": {
                **WriteResult(WriteOutcome.FAILED, reason=operation.error_message or "execution_failed").to_dict(),
                "canonical_key": "",
            }}
        return {
            "ok": False,
            "error_type": "cancelled" if operation.status == "cancelled" else "execution_failed",
            "error": operation.error_message or "工具执行失败",
            "outcome_known": bool(receipt.get("outcome_known", True)),
            "side_effect_started": bool(receipt.get("side_effect_started", False)),
            **details,
            **common,
        }
    return {
        "ok": False,
        "error_type": "execution_unknown",
        "error": "工具已持久化受理，最终状态尚未确认；请查询原 operation，禁止重复提交",
        "outcome_known": False,
        "side_effect_started": True,
        **common,
    }


class GetToolOperationStatusTool(DatabaseTool):
    @override
    def _run(self, db: Session, ctx: RunContext | None, operation_id: str) -> dict[str, Any]:
        from aiive.forget.source_policy import blocked_reference_ids
        from aiive.forget.visibility_service import ForgetVisibilityService

        run_context = require_ctx(ctx, "get_tool_operation_status")
        unavailable = {"ok": False, "error_type": "operation_not_found", "error": "工具操作不可用"}
        operation = db.query(ToolOperation).filter_by(
            id=operation_id, thread_id=run_context.thread_id,
        ).one_or_none()
        if operation is None:
            return unavailable
        turn = db.get(TurnRecord, operation.turn_record_id)
        if turn is None or (turn.thread_id, turn.turn_id) != (operation.thread_id, operation.turn_id):
            return unavailable
        visibility = ForgetVisibilityService
        if (visibility.blocked_target_ids(db, "tool_operation", [(operation.id, operation.created_at)])
                or visibility.blocked_target_ids(db, "turn_record", [(turn.id, turn.created_at)])
                or (turn.request_event_id and blocked_reference_ids(db, "event", {turn.request_event_id: None}))):
            return unavailable

        result = operation.result_payload if operation.status == "committed" else None
        if operation.capability_id == "remember_or_update" and isinstance(result, dict):
            memory_id = result.get("memory_id")
            if memory_id and result.get("outcome") != "conflict_pending" and blocked_reference_ids(
                db, "memory_record", {memory_id: None},
            ):
                return unavailable
        return {
            "ok": True,
            "operation_id": operation.id,
            "status": operation.status,
            "result": result,
            "error": operation.error_message,
            "terminal_reason": operation.terminal_reason,
        }


@with_execution_authorization
def handle_tool_operation(claimed: ClaimedJob) -> HandlerResult:
    """Outbox handler：领取持久化 operation 并执行一次真实副作用。"""
    fresh, blocked = guard_job_sources_visible(claimed, SessionLocal)
    if blocked is not None:
        return blocked
    assert fresh is not None
    claimed = fresh
    operation_id = str(claimed.payload.get("tool_operation_id", "") or "")
    if not operation_id:
        return HandlerResult(HandlerOutcome.NON_RETRYABLE, "缺少 tool_operation_id", terminal_reason="invalid_payload")

    db = SessionLocal()
    try:
        operation = db.query(ToolOperation).filter(
            ToolOperation.id == operation_id,
        ).with_for_update().one_or_none()
        if operation is None:
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "ToolOperation 不存在", terminal_reason="operation_missing")
        if operation.status == "committed":
            return HandlerResult(HandlerOutcome.COMPLETED, "operation 已提交")
        if operation.status == "cancelled":
            return HandlerResult(HandlerOutcome.COMPLETED, "operation cancelled before execution")
        if operation.status == "failed":
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, operation.error_message or "operation 已失败", terminal_reason="operation_failed")
        cancel_at = db.query(TurnRecord.cancel_requested_at).filter(TurnRecord.id == operation.turn_record_id).scalar()
        if cancel_at is not None:
            operation.cancel_requested_at = cancel_at
            if operation.status == "queued":
                operation.status = "cancelled"
                operation.completed_at = datetime.now(timezone.utc)
                operation.terminal_reason = "cancelled_before_start"
                db.add(_build_terminal_event(operation, "cancelled"))
                db.commit()
                _notify(operation.id)
                return HandlerResult(HandlerOutcome.COMPLETED, "cancelled before start")
        if operation.status in ("running", "execution_unknown") and operation.effect_mode in (
            "non_repeatable_external", "externally_reconcilable",
        ):
            operation.status = "execution_unknown"
            operation.execution_token = None
            operation.terminal_reason = "external_outcome_requires_reconciliation"
            operation.updated_at = datetime.now(timezone.utc)
            db.commit()
            _notify(operation.id)
            return HandlerResult(
                HandlerOutcome.NON_RETRYABLE,
                "不可重复外部操作的真实终态未知，需要人工核验",
                terminal_reason="external_outcome_requires_reconciliation",
            )
        if operation.descriptor_hash == "":
            return _fail_operation(db, operation, "工具定义指纹为空", "descriptor_missing")

        execution_token = str(uuid.uuid4())
        operation.status = "running"
        operation.execution_token = execution_token
        operation.attempt_count += 1
        operation.started_at = operation.started_at or datetime.now(timezone.utc)
        operation.updated_at = datetime.now(timezone.utc)
        db.commit()

        from aiive.tools.registry import get_tool_registry

        registration = get_tool_registry().get(operation.capability_id)
        if registration is None:
            return _record_failure(operation_id, execution_token, "工具未注册", "unknown_tool")
        if registration.safety.descriptor_hash != operation.descriptor_hash:
            return _record_failure(operation_id, execution_token, "工具定义在入队后发生变化", "descriptor_changed")

        context = RunContext(
            thread_id=operation.thread_id,
            trace_id=operation.trace_id,
            source=RUN_CTX_OUTBOX_WORKER,
            turn_id=operation.turn_id,
            turn_record_id=operation.turn_record_id,
        )
        if operation.capability_id == "remember_or_update":
            source_event = context.load_user_request_event(db)
            if source_event is None:
                return _record_failure(operation_id, execution_token, "user_request_event_required", "invalid_memory_source")
            context.source_event_ids = [source_event.id]
        if operation.effect_mode == "db_transactional":
            return _execute_db_transactional(operation_id, execution_token, registration, context, dict(operation.params or {}))
        return _execute_external(operation_id, execution_token, registration, context, dict(operation.params or {}))
    except Exception as error:
        db.rollback()
        logger.exception("工具 operation 领取失败: operation_id=%s", operation_id)
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, str(error), terminal_reason="operation_claim_failed")
    finally:
        db.close()


def _execute_db_transactional(
    operation_id: str,
    execution_token: str,
    handler: Any,
    context: RunContext,
    params: dict[str, Any],
) -> HandlerResult:
    """在同一事务中提交数据库业务副作用和 operation committed 回执。"""
    from aiive.tools.base import ToolCancelled, ToolExecutionContext
    from aiive.tools.executor import cancellation_requested
    if not isinstance(handler, DatabaseTool):
        return _record_failure(operation_id, execution_token, "数据库工具未声明可注入事务", "invalid_transaction_tool")

    db = SessionLocal()
    try:
        operation = db.query(ToolOperation).filter(
            ToolOperation.id == operation_id,
            ToolOperation.status == "running",
            ToolOperation.execution_token == execution_token,
        ).with_for_update().one_or_none()
        if operation is None:
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "operation fencing 失败")
        execution = ToolExecutionContext(handler.safety.capability_id, context, execution_id=operation_id,
                                         channel="turn_operation", db=db, operation_id=operation_id)
        if cancellation_requested(execution):
            raise ToolCancelled()
        coerced = coerce_tool_result(handler.execute(execution, params))
        if cancellation_requested(execution):
            raise ToolCancelled()
        if coerced.error is not None:
            # 业务结果明确失败：整个事务回滚（含业务写入）。事务归本函数所有，
            # 回滚即代表没有未提交副作用，因此未知语义在这里收敛为确定性失败。
            db.rollback()
            return _record_failure(
                operation_id, execution_token,
                f"{coerced.error.code}: {coerced.error.message}", "handler_failed",
            )
        result = coerced.value
        # handler 通过标记声明需要通知广播；广播必须在 commit 成功后执行
        needs_broadcast = bool(
            isinstance(result, dict) and result.pop("needs_notification_broadcast", False)
        )
        operation.status = "committed"
        operation.result_payload = _json_value(result)
        operation.effect_receipt = {"transactional": True, "committed": True}
        operation.error_message = None
        completed_at = datetime.now(timezone.utc)
        operation.completed_at = completed_at
        operation.updated_at = completed_at
        db.add(_build_terminal_event(operation, "committed"))
        db.commit()
        if needs_broadcast:
            _broadcast_pending_count_after_commit()
        _notify(operation_id)
        return HandlerResult(HandlerOutcome.COMPLETED, "工具副作用与回执已原子提交")
    except ToolCancelled:
        db.rollback()
        return _record_cancelled(operation_id, execution_token)
    except Exception as error:
        db.rollback()
        logger.exception("数据库工具 operation 执行失败: operation_id=%s", operation_id)
        return _record_failure(operation_id, execution_token, str(error), "handler_failed")
    finally:
        db.close()


def _execute_external(
    operation_id: str,
    execution_token: str,
    handler: Any,
    context: RunContext,
    params: dict[str, Any],
) -> HandlerResult:
    """执行外部副作用并持久化可核验回执。"""
    try:
        from aiive.tools.base import ToolExecutionContext
        from aiive.tools.executor import ToolExecutor

        # 执行前 fencing 预校验：确认自己仍持有 running + execution_token，
        # 缩小双执行窗口；已被他人接管则直接按 CLAIM_LOST 处理，不执行 handler。
        pre_db = SessionLocal()
        try:
            claimed_operation = pre_db.query(ToolOperation).filter(
                ToolOperation.id == operation_id,
                ToolOperation.status == "running",
                ToolOperation.execution_token == execution_token,
            ).one_or_none()
        finally:
            pre_db.close()
        if claimed_operation is None:
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "operation fencing 失败（执行前预校验）")

        response = ToolExecutor.execute_direct(handler, ToolExecutionContext(handler.safety.capability_id, context,
                                               execution_id=operation_id, channel="turn_operation", operation_id=operation_id),
                                               params, handler.safety.timeout_seconds or 60)
        if not response.get("ok"):
            # execute_direct 已经按统一契约归一化业务失败，这里只做状态映射。
            if outcome_is_unknown(response):
                return _record_unknown(operation_id, execution_token, str(response.get("error")))
            if response.get("error_type") == "cancelled":
                return _record_cancelled(operation_id, execution_token)
            return _record_failure(
                operation_id, execution_token, str(response.get("error")), "tool_failed",
                side_effect_started=bool(response.get("side_effect_started", False)),
            )
        result = response.get("result")
        db = SessionLocal()
        try:
            operation = db.query(ToolOperation).filter(
                ToolOperation.id == operation_id,
                ToolOperation.status == "running",
                ToolOperation.execution_token == execution_token,
            ).with_for_update().one_or_none()
            if operation is None:
                return HandlerResult(HandlerOutcome.CLAIM_LOST, "operation fencing 失败")
            operation.status = "committed"
            operation.result_payload = _json_value(result)
            operation.effect_receipt = {"transactional": False, "handler_returned": True}
            completed_at = datetime.now(timezone.utc)
            operation.completed_at = completed_at
            operation.updated_at = completed_at
            db.add(_build_terminal_event(operation, "committed"))
            db.commit()
        finally:
            db.close()
        _notify(operation_id)
        return HandlerResult(HandlerOutcome.COMPLETED, "外部副作用已执行并记录回执")
    except Exception as error:
        logger.exception("外部工具 operation 执行失败: operation_id=%s", operation_id)
        operation = _load_operation(operation_id)
        if operation is not None and operation.effect_mode in {"non_repeatable_external", "externally_reconcilable"}:
            return _record_unknown(operation_id, execution_token, str(error))
        return _record_failure(operation_id, execution_token, str(error), "handler_failed")


def _broadcast_pending_count_after_commit() -> None:
    """在业务事务 commit 成功后，用独立会话推送最新 pending 通知计数。"""
    db = SessionLocal()
    try:
        from aiive.api.routes_notifications import broadcast_pending_count

        broadcast_pending_count(db)
    except Exception:
        logger.exception("commit 后广播通知计数失败")
    finally:
        db.close()


def _build_terminal_event(operation: ToolOperation, status: str) -> Event:
    """构造 operation 终态追加事件，历史超时事实不原地篡改。"""
    return Event(
        id=str(uuid.uuid4()),
        trace_id=operation.trace_id,
        thread_id=operation.thread_id,
        event_type="tool_operation_terminal",
        turn_id=operation.turn_id,
        payload={
            "operation_id": operation.id,
            "tool_call_id": operation.tool_call_id,
            "name": operation.capability_id,
            "status": status,
            "result": operation.result_payload if status == "committed" else None,
            "error": operation.error_message,
        },
    )


def _record_unknown(operation_id: str, execution_token: str, error: str) -> HandlerResult:
    """记录不可重复外部副作用的未知终态，禁止 Worker 自动重放。"""
    db = SessionLocal()
    try:
        operation = db.query(ToolOperation).filter(
            ToolOperation.id == operation_id,
            ToolOperation.status == "running",
            ToolOperation.execution_token == execution_token,
        ).with_for_update().one_or_none()
        if operation is None:
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "operation fencing 失败")
        operation.status = "execution_unknown"
        operation.error_message = error[:2000]
        operation.effect_receipt = {"outcome_known": False, "side_effect_started": True}
        operation.terminal_reason = "external_outcome_requires_reconciliation"
        operation.execution_token = None
        operation.updated_at = datetime.now(timezone.utc)
        db.add(_build_terminal_event(operation, "execution_unknown"))
        db.commit()
        _notify(operation_id)
        return HandlerResult(
            HandlerOutcome.NON_RETRYABLE,
            "外部副作用真实终态未知，需要人工核验",
            terminal_reason="external_outcome_requires_reconciliation",
        )
    finally:
        db.close()


def _record_cancelled(operation_id: str, execution_token: str) -> HandlerResult:
    """Called only before start or after a confirmed transaction rollback/stop."""
    with SessionLocal() as db:
        operation = db.query(ToolOperation).filter(
            ToolOperation.id == operation_id, ToolOperation.status == "running",
            ToolOperation.execution_token == execution_token,
        ).with_for_update().one_or_none()
        if operation is None:
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "operation fencing failed")
        now = datetime.now(timezone.utc)
        operation.status = "cancelled"
        operation.cancel_requested_at = operation.cancel_requested_at or now
        operation.completed_at = now
        operation.terminal_reason = "execution_cancelled"
        db.add(_build_terminal_event(operation, "cancelled"))
        db.commit()
    _notify(operation_id)
    return HandlerResult(HandlerOutcome.COMPLETED, "execution cancelled")


def _record_failure(
    operation_id: str, execution_token: str, error: str, reason: str,
    *, side_effect_started: bool = False,
) -> HandlerResult:
    """使用独立短事务记录确定性失败终态。"""
    db = SessionLocal()
    try:
        operation = db.query(ToolOperation).filter(
            ToolOperation.id == operation_id,
            ToolOperation.status == "running",
            ToolOperation.execution_token == execution_token,
        ).with_for_update().one_or_none()
        if operation is None:
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "operation fencing 失败")
        return _fail_operation(
            db, operation, error, reason, side_effect_started=side_effect_started,
        )
    finally:
        db.close()


def _fail_operation(
    db: Session, operation: ToolOperation, error: str, reason: str,
    *, side_effect_started: bool = False,
) -> HandlerResult:
    """提交失败终态并通知等待者。"""
    operation.status = "failed"
    operation.error_message = error[:2000]
    # 确定性失败 ≠ 没有副作用：已经执行过的外部操作必须保留这个事实。
    operation.effect_receipt = {
        "outcome_known": True,
        "side_effect_started": side_effect_started,
    }
    operation.terminal_reason = reason
    completed_at = datetime.now(timezone.utc)
    operation.completed_at = completed_at
    operation.updated_at = completed_at
    db.add(_build_terminal_event(operation, "failed"))
    db.commit()
    _notify(operation.id)
    return HandlerResult(HandlerOutcome.NON_RETRYABLE, error, terminal_reason=reason)
