"""统一审批服务的 Persistent Task 分支。"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from aiive.control.capability_broker import CapabilityBroker
from aiive.db.models import AgentAction, AgentTask, ApprovalRequest
from aiive.task_runtime.repository import TaskRepository, canonical_json, utcnow
from aiive.task_runtime.schemas import ActionStatus, TaskStatus


def _approval_hash(action: AgentAction, expires_at: datetime, checkpoint_id: str) -> str:
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    material = {
        "action_id": action.id,
        "task_id": action.task_id,
        "capability_id": action.capability_id,
        "target_node_id": action.target_node_id,
        "idempotency_key": action.idempotency_key,
        "checkpoint_id": checkpoint_id,
        "arguments_hash": action.arguments_hash,
        "descriptor_hash": action.descriptor_hash,
        "preconditions": action.preconditions or {},
        "effects": action.effects or {},
        "expires_at": expires_at.isoformat(),
    }
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


class TaskApprovalService:
    def __init__(self, db: Session):
        self.db: Session = db
        self.repo: TaskRepository = TaskRepository(db)

    def create(self, task: AgentTask, action: AgentAction, *, ttl_hours: int = 24) -> ApprovalRequest:
        if ttl_hours < 1 or ttl_hours > 168:
            raise ValueError("approval_ttl_out_of_range")
        existing = self.db.query(ApprovalRequest).filter(
            ApprovalRequest.action_id == action.id,
        ).one_or_none()
        if existing is not None:
            return existing
        expires_at = utcnow() + timedelta(hours=ttl_hours)
        checkpoint = self.repo.latest_checkpoint(task.id)
        if checkpoint is None:
            raise ValueError("approval_requires_checkpoint")
        approval = ApprovalRequest(
            thread_id=task.thread_id,
            task_id=task.id,
            action_id=action.id,
            checkpoint_id=checkpoint.id,
            turn_record_id=None,
            turn_id=None,
            trace_id=task.id,
            tool_call_id=None,
            tool_name=action.capability_id,
            tool_args=action.arguments or {},
            tool_args_hash=action.arguments_hash,
            descriptor_hash=action.descriptor_hash,
            risk_snapshot={
                "risk_level": action.risk_level,
                "requires_approval": True,
                "target_node_id": action.target_node_id,
            },
            preconditions=action.preconditions or {},
            effects=action.effects or {},
            approval_hash=_approval_hash(action, expires_at, checkpoint.id),
            expires_at=expires_at,
            status="pending",
        )
        self.db.add(approval)
        self.db.flush()
        return approval

    def decide(self, approval_id: str, decision: str) -> dict[str, Any]:
        approval = self.db.query(ApprovalRequest).filter(
            ApprovalRequest.id == approval_id,
            ApprovalRequest.task_id.is_not(None),
        ).with_for_update().one_or_none()
        if approval is None:
            raise ValueError("task_approval_not_found")
        if approval.status != "pending":
            expected = "denied" if decision == "deny" else "succeeded"
            if approval.status == expected:
                return self._payload(approval, idempotent=True)
            raise ValueError(f"approval_already_{approval.status}")
        task = self.repo.get_task(str(approval.task_id), for_update=True)
        action = self.db.get(AgentAction, approval.action_id)
        if task is None or action is None:
            raise ValueError("approval_owner_missing")
        if task.status != TaskStatus.BLOCKED_APPROVAL.value:
            raise ValueError(f"task_not_waiting_approval:{task.status}")
        if action.status != ActionStatus.AWAITING_APPROVAL.value:
            raise ValueError(f"action_not_waiting_approval:{action.status}")
        now = utcnow()
        expires = approval.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)

        if decision == "deny":
            approval.status = "denied"
            approval.decision = "deny"
            approval.decided_at = now
            approval.completed_at = now
            self.repo.transition_action(action, ActionStatus.CANCELLED.value, error="user_denied")
            report = {
                "status": "failed",
                "summary": "用户拒绝了完成任务所需的高风险操作。",
                "completed_actions": action.sequence - 1,
                "evidence_refs": [], "artifact_refs": [],
                "risks": [], "unresolved": [action.capability_id],
            }
            self.repo.transition_task(
                task, TaskStatus.FAILED.value, report=report,
                event_type="approval_denied",
                event_payload={"approval_id": approval.id, "action_id": action.id},
                visibility="conversation",
            )
            self.repo.checkpoint(task.id, reason="approval_denied")
            self.db.flush()
            return self._payload(approval, idempotent=False)

        if expires is not None and expires <= now:
            self._expire(approval, task, action, now)
            return self._payload(approval, idempotent=False)

        expected_hash = _approval_hash(action, expires or now, str(approval.checkpoint_id or ""))
        prepared = CapabilityBroker(self.db).prepare(task, action.capability_id, action.arguments or {})
        precondition_issues = CapabilityBroker.validate_preconditions(action)
        integrity_ok = (
            action.arguments_hash == approval.tool_args_hash
            and hashlib.sha256(canonical_json(approval.tool_args or {}).encode("utf-8")).hexdigest()
            == approval.tool_args_hash
            and action.descriptor_hash == approval.descriptor_hash
            and canonical_json(approval.preconditions or {}) == canonical_json(action.preconditions or {})
            and canonical_json(approval.effects or {}) == canonical_json(action.effects or {})
            and (approval.risk_snapshot or {}).get("risk_level") == action.risk_level
            and (approval.risk_snapshot or {}).get("target_node_id") == action.target_node_id
            and approval.approval_hash == expected_hash
            and prepared.registration is not None
            and prepared.registration.safety.descriptor_hash == action.descriptor_hash
            and prepared.decision.outcome == "approval"
            and not precondition_issues
        )
        if not integrity_ok:
            approval.status = "failed"
            approval.decision = "approve"
            approval.error_message = "approval_invalidated"
            approval.decided_at = now
            approval.completed_at = now
            self.repo.transition_action(action, ActionStatus.INVALIDATED.value, error="approval_invalidated")
            self.repo.transition_task(task, TaskStatus.QUEUED.value)
            self.repo.enqueue_run(task, trigger="approval_invalidated_replan")
            self.repo.append_event(
                task.id, "action_invalidated",
                {"approval_id": approval.id, "action_id": action.id, "issues": precondition_issues},
            )
            self.repo.checkpoint(task.id, reason="approval_invalidated")
            self.db.flush()
            return self._payload(approval, idempotent=False)

        approval.status = "succeeded"
        approval.decision = "approve"
        approval.decided_at = now
        approval.completed_at = now
        approval.execution_result = {"authorization": "granted", "action_id": action.id}
        self.repo.transition_action(action, ActionStatus.READY.value)
        self.repo.transition_task(task, TaskStatus.QUEUED.value)
        self.repo.enqueue_run(task, trigger="approval_granted")
        self.repo.append_event(
            task.id, "approval_granted",
            {"approval_id": approval.id, "action_id": action.id},
            visibility="conversation",
        )
        self.repo.checkpoint(task.id, reason="approval_granted", pending_action_refs=[action.id])
        self.db.flush()
        return self._payload(approval, idempotent=False)

    def expire_due(self, *, now: datetime | None = None, limit: int = 100) -> int:
        """确定性回收过期审批，避免 Task 永久停在 blocked_approval。"""
        now = now or utcnow()
        approvals = self.db.query(ApprovalRequest).filter(
            ApprovalRequest.task_id.is_not(None),
            ApprovalRequest.status == "pending",
            ApprovalRequest.expires_at.is_not(None),
            ApprovalRequest.expires_at <= now,
        ).order_by(ApprovalRequest.expires_at.asc()).limit(limit).with_for_update(skip_locked=True).all()
        expired = 0
        for approval in approvals:
            task = self.repo.get_task(str(approval.task_id), for_update=True)
            action = self.db.get(AgentAction, approval.action_id)
            if (
                task is not None
                and action is not None
                and task.status == TaskStatus.BLOCKED_APPROVAL.value
                and action.status == ActionStatus.AWAITING_APPROVAL.value
            ):
                self._expire(approval, task, action, now)
            else:
                approval.status = "failed"
                approval.error_message = "approval_expired_owner_inactive"
                approval.completed_at = now
            expired += 1
        self.db.flush()
        return expired

    def _expire(
        self,
        approval: ApprovalRequest,
        task: AgentTask,
        action: AgentAction,
        now: datetime,
    ) -> None:
        approval.status = "failed"
        approval.error_message = "approval_expired"
        approval.completed_at = now
        self.repo.transition_action(action, ActionStatus.INVALIDATED.value, error="approval_expired")
        self.repo.transition_task(task, TaskStatus.QUEUED.value)
        self.repo.enqueue_run(task, trigger="approval_expired_replan")
        self.repo.append_event(
            task.id,
            "approval_expired",
            {"approval_id": approval.id, "action_id": action.id},
            visibility="conversation",
        )
        self.repo.checkpoint(task.id, reason="approval_expired")
        self.db.flush()

    @staticmethod
    def _payload(approval: ApprovalRequest, *, idempotent: bool) -> dict[str, Any]:
        return {
            "ok": approval.status in {"succeeded", "denied"},
            "action": approval.status,
            "approval_id": approval.id,
            "task_id": approval.task_id,
            "action_id": approval.action_id,
            "tool_name": approval.tool_name,
            "trace_id": approval.trace_id,
            "error": approval.error_message,
            "idempotent_replay": idempotent,
        }
