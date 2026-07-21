"""副作用工具的持久化入队、有限等待和 Worker 终态提交。"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aiive.context.run_context import RunContext
from aiive.db.base import SessionLocal
from aiive.db.models import Event, OutboxJob, ToolOperation
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome, HandlerResult

logger = logging.getLogger(__name__)
TOOL_OPERATION_JOB_TYPE = "tool_operation"
_TERMINAL_STATUSES = frozenset({"committed", "failed"})
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
    operation = _load_operation(operation_id)
    if operation is not None and operation.status not in _TERMINAL_STATUSES:
        event.wait(timeout=max(0.0, timeout_seconds))
        operation = _load_operation(operation_id)
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


def operation_response(operation: ToolOperation) -> dict[str, Any]:
    """将持久化状态转换为统一 Registry 响应。"""
    common = {
        "operation_id": operation.id,
        "execution_status": operation.status,
    }
    if operation.status == "committed":
        return {"ok": True, "result": operation.result_payload, **common}
    if operation.status == "failed":
        return {
            "ok": False,
            "error_type": "execution_failed",
            "error": operation.error_message or "工具执行失败",
            **common,
        }
    return {
        "ok": False,
        "error_type": "execution_unknown",
        "error": "工具已持久化受理，最终状态尚未确认；请查询原 operation，禁止重复提交",
        **common,
    }


def handle_tool_operation(claimed: ClaimedJob) -> HandlerResult:
    """Outbox handler：领取持久化 operation 并执行一次真实副作用。"""
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
        if operation.status == "failed":
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, operation.error_message or "operation 已失败", terminal_reason="operation_failed")
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
            source="outbox_worker",
            turn_id=operation.turn_id,
            turn_record_id=operation.turn_record_id,
        )
        if operation.effect_mode == "db_transactional":
            return _execute_db_transactional(operation_id, execution_token, registration.handler, context, dict(operation.params or {}))
        return _execute_external(operation_id, execution_token, registration.handler, context, dict(operation.params or {}))
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
    original = getattr(handler, "_aiive_db_handler", None)
    accepts_ctx = bool(getattr(handler, "_aiive_accepts_ctx", False))
    if original is None:
        return _record_failure(operation_id, execution_token, "数据库工具未声明可注入事务", "invalid_db_handler")

    db = SessionLocal()
    try:
        operation = db.query(ToolOperation).filter(
            ToolOperation.id == operation_id,
            ToolOperation.status == "running",
            ToolOperation.execution_token == execution_token,
        ).with_for_update().one_or_none()
        if operation is None:
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "operation fencing 失败")
        result = original(db, context, **params) if accepts_ctx else original(db, **params)
        if isinstance(result, dict) and result.get("ok") is False:
            raise RuntimeError(str(result.get("error", "工具返回失败")))
        operation.status = "committed"
        operation.result_payload = _json_value(result)
        operation.effect_receipt = {"transactional": True, "committed": True}
        operation.error_message = None
        operation.completed_at = datetime.now(timezone.utc)
        operation.updated_at = operation.completed_at
        db.add(_build_terminal_event(operation, "committed"))
        db.commit()
        _notify(operation_id)
        return HandlerResult(HandlerOutcome.COMPLETED, "工具副作用与回执已原子提交")
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
        import inspect

        result = handler(ctx=context, **params) if "ctx" in inspect.signature(handler).parameters else handler(**params)
        if isinstance(result, dict) and result.get("ok") is False:
            raise RuntimeError(str(result.get("error", "工具返回失败")))
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
            operation.completed_at = datetime.now(timezone.utc)
            operation.updated_at = operation.completed_at
            db.add(_build_terminal_event(operation, "committed"))
            db.commit()
        finally:
            db.close()
        _notify(operation_id)
        return HandlerResult(HandlerOutcome.COMPLETED, "外部副作用已执行并记录回执")
    except Exception as error:
        logger.exception("外部工具 operation 执行失败: operation_id=%s", operation_id)
        operation = _load_operation(operation_id)
        if operation is not None and operation.effect_mode == "non_repeatable_external":
            return _record_unknown(operation_id, execution_token, str(error))
        return _record_failure(operation_id, execution_token, str(error), "handler_failed")


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


def _record_failure(operation_id: str, execution_token: str, error: str, reason: str) -> HandlerResult:
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
        return _fail_operation(db, operation, error, reason)
    finally:
        db.close()


def _fail_operation(db: Session, operation: ToolOperation, error: str, reason: str) -> HandlerResult:
    """提交失败终态并通知等待者。"""
    operation.status = "failed"
    operation.error_message = error[:2000]
    operation.terminal_reason = reason
    operation.completed_at = datetime.now(timezone.utc)
    operation.updated_at = operation.completed_at
    db.add(_build_terminal_event(operation, "failed"))
    db.commit()
    _notify(operation.id)
    return HandlerResult(HandlerOutcome.NON_RETRYABLE, error, terminal_reason=reason)
