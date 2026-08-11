"""工具审批接口：以持久化审批记录作为唯一事实源。"""
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, ClassVar, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func

from aiive.context.run_context import RunContext, RUN_CTX_TRUSTED_APPROVAL
from aiive.db.base import SessionLocal
from aiive.db.models import ApprovalRequest, Event, Thread, TurnRecord
from aiive.runtime.working_state import WorkingStateService
from aiive.tools.registry import get_tool_registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


class ApprovalRespondRequest(BaseModel):
    """审批响应只包含服务端审批 ID 和用户决策。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    approval_id: str
    action: Literal["approve", "deny"]


def _terminal_response(approval: ApprovalRequest) -> dict[str, Any]:
    """从审批终态构造幂等响应，不再次执行工具。"""
    result = approval.execution_result or {}
    return {
        "ok": approval.status in ("succeeded", "denied"),
        "action": approval.status,
        "approval_id": approval.id,
        "tool_name": approval.tool_name,
        "tool_result": result,
        "trace_id": approval.trace_id,
        "error": approval.error_message,
        "idempotent_replay": True,
    }


@router.post("/approval/respond")
def respond_approval(request: ApprovalRespondRequest) -> dict[str, Any]:
    """原子处理审批决策，并且只执行服务端冻结的工具调用。"""
    execution_token = str(uuid.uuid4())
    ws = WorkingStateService()

    db = SessionLocal()
    try:
        approval = db.get(ApprovalRequest, request.approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="审批记录不存在")

        # Persistent Task 审批只授予冻结 Action 的执行权并重新入队；真实执行在
        # 新 AgentRun 中发生，避免审批 HTTP 请求承担长任务或重复副作用。
        if approval.task_id is not None:
            from aiive.control.approval_service import TaskApprovalService

            try:
                payload = TaskApprovalService(db).decide(request.approval_id, request.action)
                db.commit()
                return payload
            except ValueError as error:
                db.rollback()
                message = str(error)
                status = 404 if message == "task_approval_not_found" else 409
                raise HTTPException(status_code=status, detail=message) from error

        if approval.status != "pending":
            if request.action == "deny" and approval.status == "denied":
                return _terminal_response(approval)
            if request.action == "approve" and approval.status in (
                "succeeded", "failed", "interrupted_unknown",
            ):
                return _terminal_response(approval)
            raise HTTPException(status_code=409, detail=f"审批已处于 {approval.status} 状态")

        now = datetime.now(timezone.utc)
        if request.action == "deny":
            affected = db.query(ApprovalRequest).filter(
                ApprovalRequest.id == request.approval_id,
                ApprovalRequest.status == "pending",
            ).update({
                ApprovalRequest.status: "denied",
                ApprovalRequest.decision: "deny",
                ApprovalRequest.decided_at: now,
                ApprovalRequest.completed_at: now,
                ApprovalRequest.updated_at: now,
            }, synchronize_session=False)
            if affected != 1:
                db.rollback()
                raise HTTPException(status_code=409, detail="审批已被其他请求处理")
            ws.remove_pending_approval(db, approval.thread_id, approval.id)
            db.commit()
            return {
                "ok": True,
                "action": "denied",
                "approval_id": approval.id,
                "tool_name": approval.tool_name,
                "trace_id": approval.trace_id,
                "idempotent_replay": False,
            }

        affected = db.query(ApprovalRequest).filter(
            ApprovalRequest.id == request.approval_id,
            ApprovalRequest.status == "pending",
        ).update({
            ApprovalRequest.status: "executing",
            ApprovalRequest.decision: "approve",
            ApprovalRequest.execution_token: execution_token,
            ApprovalRequest.decided_at: now,
            ApprovalRequest.updated_at: now,
        }, synchronize_session=False)
        if affected != 1:
            db.rollback()
            raise HTTPException(status_code=409, detail="审批已被其他请求处理")
        db.commit()

        tool_name = approval.tool_name
        tool_args = dict(approval.tool_args or {})
        canonical_args = json.dumps(
            tool_args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        )
        actual_args_hash = hashlib.sha256(canonical_args.encode("utf-8")).hexdigest()
        if actual_args_hash != approval.tool_args_hash:
            db.query(ApprovalRequest).filter(
                ApprovalRequest.id == approval.id,
                ApprovalRequest.status == "executing",
                ApprovalRequest.execution_token == execution_token,
            ).update({
                ApprovalRequest.status: "failed",
                ApprovalRequest.error_message: "审批参数快照完整性校验失败",
                ApprovalRequest.completed_at: now,
                ApprovalRequest.updated_at: now,
            }, synchronize_session=False)
            ws.remove_pending_approval(db, approval.thread_id, approval.id)
            db.commit()
            raise HTTPException(status_code=409, detail="审批参数快照完整性校验失败")
        descriptor_hash = approval.descriptor_hash
        risk_snapshot = dict(approval.risk_snapshot or {})
        thread_id = approval.thread_id
        turn_record_id = approval.turn_record_id
        turn_id = approval.turn_id
        trace_id = approval.trace_id
        tool_call_id = approval.tool_call_id
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("领取审批执行权失败: approval_id=%s", request.approval_id)
        raise HTTPException(status_code=500, detail="审批状态迁移失败")
    finally:
        db.close()

    if trace_id is None or turn_id is None or turn_record_id is None or tool_call_id is None:
        raise HTTPException(status_code=409, detail="旧审批缺少 Turn 执行身份，无法安全执行")

    if risk_snapshot.get("executor_kind") == "desktop_node":
        registry_db = SessionLocal()
        try:
            from aiive.desktop.registry_overlay import build_registry_for_thread

            registry = build_registry_for_thread(registry_db, thread_id)
        finally:
            registry_db.close()
        registration = registry.get(tool_name)
        if (
            registration is None
            or registration.executor_kind != "desktop_node"
            or registration.executor_node_id != risk_snapshot.get("executor_node_id")
        ):
            result = {
                "ok": False,
                "error": "原审批绑定的桌面节点已离线或发生变化",
                "error_type": "execution_unknown",
            }
        else:
            result = registry.execute_approved(
                tool_name,
                tool_args,
                descriptor_hash,
                RunContext(
                    thread_id=thread_id,
                    trace_id=trace_id,
                    source=RUN_CTX_TRUSTED_APPROVAL,
                    turn_id=turn_id,
                    turn_record_id=turn_record_id,
                ),
                tool_call_id=tool_call_id,
            )
    else:
        registry = get_tool_registry()
        result = registry.execute_approved(
            tool_name,
            tool_args,
            descriptor_hash,
            RunContext(
                thread_id=thread_id,
                trace_id=trace_id,
                source=RUN_CTX_TRUSTED_APPROVAL,
                turn_id=turn_id,
                turn_record_id=turn_record_id,
            ),
            tool_call_id=tool_call_id,
        )

    error_type = str(result.get("error_type", "") or "")
    if result.get("ok"):
        final_status = "succeeded"
    elif error_type == "execution_unknown":
        final_status = "interrupted_unknown"
    else:
        final_status = "failed"

    db = SessionLocal()
    try:
        approval = db.query(ApprovalRequest).filter(
            ApprovalRequest.id == request.approval_id,
        ).with_for_update().one_or_none()
        if approval is None:
            raise RuntimeError("审批记录在执行后不存在")
        if approval.status != "executing" or approval.execution_token != execution_token:
            raise RuntimeError("审批执行 fencing 校验失败")

        turn = db.get(TurnRecord, turn_record_id)
        if turn is None or turn.thread_id != thread_id or turn.turn_id != turn_id:
            raise RuntimeError("审批来源 Turn 不存在或关联不一致")

        db.query(Thread).filter(Thread.id == thread_id).with_for_update().one()
        max_index = db.query(func.max(Event.turn_event_index)).filter(
            Event.thread_id == thread_id,
            Event.turn_id == turn_id,
        ).scalar() or 0
        call_event_id = str(uuid.uuid4())
        result_event_id = str(uuid.uuid4())
        db.add(Event(
            id=call_event_id,
            trace_id=trace_id,
            thread_id=thread_id,
            event_type="tool_call",
            turn_id=turn_id,
            turn_event_index=max_index + 1,
            payload={
                "name": tool_name,
                "params": tool_args,
                "tool_call_id": tool_call_id,
                "approval_id": approval.id,
            },
        ))
        db.add(Event(
            id=result_event_id,
            trace_id=trace_id,
            thread_id=thread_id,
            event_type="tool_result",
            turn_id=turn_id,
            turn_event_index=max_index + 2,
            payload={
                "name": tool_name,
                "result": result,
                "status": (
                    "completed" if result.get("ok")
                    else "execution_unknown" if error_type == "execution_unknown"
                    else "failed"
                ),
                "tool_call_id": tool_call_id,
                "approval_id": approval.id,
            },
        ))

        now = datetime.now(timezone.utc)
        approval.status = final_status
        approval.execution_result = result
        approval.result_event_id = result_event_id
        approval.error_message = None if result.get("ok") else str(result.get("error", "工具执行失败"))
        approval.completed_at = now
        approval.updated_at = now
        ws.remove_pending_approval(db, thread_id, approval.id)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("提交审批执行结果失败: approval_id=%s", request.approval_id)
        raise HTTPException(
            status_code=500,
            detail="工具可能已执行，但结果提交失败；审批保持执行中，禁止重复执行",
        )
    finally:
        db.close()

    return {
        "ok": bool(result.get("ok")),
        "action": final_status,
        "approval_id": request.approval_id,
        "tool_name": tool_name,
        "tool_result": result,
        "trace_id": trace_id,
        "error": None if result.get("ok") else result.get("error", "工具执行失败"),
        "idempotent_replay": False,
    }
