"""Desktop Node journal 的服务端投影与 unknown Action 对账。"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import AgentAction, DesktopActionReceipt
from aiive.desktop.protocol import DesktopJournalEntry
from aiive.task_runtime.evidence import EvidenceStore
from aiive.task_runtime.repository import TaskRepository, utcnow
from aiive.task_runtime.schemas import ActionStatus, TaskStatus


def _hash_result(value: Any) -> str:
    # 与 Desktop Node 的 stableStringify 保持逐字节一致，才能可靠验证 journal。
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class DesktopReconciliationService:
    def outstanding_action_ids(self, db: Session, node_id: str) -> list[str]:
        return [row[0] for row in db.query(AgentAction.id).filter(
            AgentAction.target_node_id == node_id,
            AgentAction.status.in_([
                ActionStatus.DISPATCHED.value, ActionStatus.RUNNING.value, ActionStatus.UNKNOWN.value,
            ]),
        ).order_by(AgentAction.created_at.asc()).limit(500).all()]

    def record_ack(self, db: Session, node_id: str, action_id: str, status: str) -> None:
        action = db.get(AgentAction, action_id)
        if action is None or action.target_node_id != node_id:
            return
        receipt = db.get(DesktopActionReceipt, action_id)
        if receipt is None:
            receipt = DesktopActionReceipt(
                action_id=action.id,
                node_id=node_id,
                idempotency_key=action.idempotency_key,
                arguments_hash=action.arguments_hash,
                status="received",
            )
            db.add(receipt)
        if status in {"received", "started"}:
            receipt.status = status
            if status == "started" and receipt.node_started_at is None:
                receipt.node_started_at = utcnow()
            if status == "started" and action.status == ActionStatus.DISPATCHED.value:
                TaskRepository(db).transition_action(
                    action, ActionStatus.RUNNING.value, payload={"node_ack": "started"},
                )
        db.flush()

    def apply_entry(
        self,
        db: Session,
        node_id: str,
        raw: dict[str, Any],
        *,
        reconcile_action: bool = True,
    ) -> bool:
        entry = DesktopJournalEntry.model_validate(raw)
        action = db.get(AgentAction, entry.action_id)
        if action is None or action.target_node_id != node_id:
            return False
        if action.idempotency_key != entry.idempotency_key or action.arguments_hash != entry.arguments_hash:
            return False
        if entry.result_hash and _hash_result(entry.result) != entry.result_hash:
            return False
        receipt = db.get(DesktopActionReceipt, action.id)
        if receipt is None:
            receipt = DesktopActionReceipt(
                action_id=action.id,
                node_id=node_id,
                idempotency_key=action.idempotency_key,
                arguments_hash=action.arguments_hash,
                status="unknown",
            )
            db.add(receipt)
        mapped = {
            "received": "received", "started": "started",
            "committed": "committed", "failed": "failed",
        }.get(entry.status, "unknown")
        receipt.status = mapped
        receipt.result_hash = entry.result_hash or None
        receipt.result_payload = entry.result if isinstance(entry.result, dict) else {"value": entry.result}
        receipt.error_message = entry.error or None
        if mapped in {"committed", "failed"}:
            receipt.node_completed_at = utcnow()
        db.flush()

        if not reconcile_action:
            return True

        if action.status not in {
            ActionStatus.UNKNOWN.value, ActionStatus.DISPATCHED.value, ActionStatus.RUNNING.value,
        }:
            return True
        repo = TaskRepository(db)
        task = repo.get_task(action.task_id, for_update=True)
        if task is None:
            return False
        if mapped == "committed":
            evidence = EvidenceStore().capture_result(
                db,
                task_id=task.id,
                run_id=action.execution_run_id or action.run_id,
                action=action,
                result={"ok": True, "result": entry.result, "reconciled": True},
                kind="desktop_reconciliation",
            )
            repo.transition_action(
                action, ActionStatus.SUCCEEDED.value,
                payload={"evidence_id": evidence.id, "reconciled": True},
            )
        elif mapped == "failed":
            repo.transition_action(
                action, ActionStatus.FAILED.value,
                error=entry.error or "desktop_action_failed",
                payload={"reconciled": True},
            )
        else:
            return True

        from aiive.task_runtime.resource_locks import ResourceLockService

        ResourceLockService().release_action(db, task_id=task.id, action_id=action.id)

        remaining = db.query(AgentAction).filter(
            AgentAction.task_id == task.id,
            AgentAction.status.in_([
                ActionStatus.DISPATCHED.value, ActionStatus.RUNNING.value, ActionStatus.UNKNOWN.value,
            ]),
        ).count()
        if remaining == 0 and task.status in {TaskStatus.RECONCILING.value, TaskStatus.BLOCKED_NODE.value}:
            repo.transition_task(task, TaskStatus.QUEUED.value)
            repo.enqueue_run(task, trigger="desktop_reconciled")
            repo.append_event(
                task.id, "desktop_action_reconciled",
                {"action_id": action.id, "status": mapped}, visibility="conversation",
            )
            repo.checkpoint(task.id, reason="desktop_reconciled")
        return True

    def apply_missing(self, db: Session, node_id: str, action_id: str) -> bool:
        """Node 明确报告 Journal 无此 Action 时，仅在从未 ACK 的情况下安全重派。"""
        action = db.get(AgentAction, action_id)
        if action is None or action.target_node_id != node_id or action.status != ActionStatus.UNKNOWN.value:
            return False
        receipt = db.get(DesktopActionReceipt, action.id)
        repo = TaskRepository(db)
        task = repo.get_task(action.task_id, for_update=True)
        if task is None:
            return False
        if task.status in {
            TaskStatus.SUCCEEDED.value, TaskStatus.PARTIAL.value,
            TaskStatus.FAILED.value, TaskStatus.CANCELLED.value,
        }:
            repo.transition_action(
                action, ActionStatus.CANCELLED.value,
                payload={"reason": "task_already_terminal"},
            )
            return True
        if receipt is not None and receipt.status in {"received", "started", "committed", "failed"}:
            repo.append_event(
                task.id,
                "desktop_journal_inconsistent",
                {"action_id": action.id, "server_receipt": receipt.status},
                visibility="conversation",
            )
            repo.checkpoint(task.id, reason="desktop_journal_inconsistent", pending_action_refs=[action.id])
            return False
        repo.transition_action(
            action, ActionStatus.READY.value,
            payload={"reason": "node_confirmed_not_executed"},
        )
        from aiive.task_runtime.resource_locks import ResourceLockService

        ResourceLockService().release_action(db, task_id=task.id, action_id=action.id)
        if task.status in {TaskStatus.RECONCILING.value, TaskStatus.BLOCKED_NODE.value}:
            repo.transition_task(task, TaskStatus.QUEUED.value)
            repo.enqueue_run(task, trigger="desktop_confirmed_not_executed")
        repo.append_event(
            task.id,
            "desktop_action_not_executed",
            {"action_id": action.id},
            visibility="conversation",
        )
        repo.checkpoint(task.id, reason="desktop_not_executed", pending_action_refs=[action.id])
        return True


desktop_reconciliation_service = DesktopReconciliationService()
