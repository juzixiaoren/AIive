"""Resolve an unknown, non-Desktop action only after an explicit user check."""
from sqlalchemy.orm import Session

from aiive.db.models import AgentAction
from aiive.task_runtime.evidence import EvidenceStore
from aiive.task_runtime.read_repository import TaskReadRepository
from aiive.task_runtime.repository import TERMINAL_TASK_STATES, TaskRepository
from aiive.task_runtime.resource_locks import ResourceLockService
from aiive.task_runtime.schemas import ActionStatus, TaskStatus


def resolve_unknown_action(
    db: Session, task_id: str, action_id: str, resolution: str, observation: str, attempt_id: str,
) -> dict[str, str]:
    repo = TaskRepository(db)
    reader = TaskReadRepository(db)
    task = reader.get_task(task_id, for_update=True)
    action = db.get(AgentAction, action_id, populate_existing=True)
    if task is None or action is None or action.task_id != task_id or not reader.visible_child("agent_action", action):
        raise LookupError("task_action_unavailable")
    if action.target_node_id:
        raise ValueError("desktop_action_requires_journal")
    if attempt_id != (action.execution_token or action.id):
        raise ValueError("action_attempt_changed")
    if action.status != ActionStatus.UNKNOWN.value:
        raise ValueError("action_no_longer_unknown")
    terminal = task.status in TERMINAL_TASK_STATES
    if not terminal and task.status != TaskStatus.RECONCILING.value:
        raise ValueError("task_not_reconciling")
    if resolution == "completed":
        evidence = EvidenceStore().capture_result(
            db, task_id=task.id, run_id=action.execution_run_id or action.run_id, action=action,
            result={"ok": True, "confirmed_by": "user", "observation": observation},
            kind="user_reconciliation",
        )
        repo.transition_action(action, ActionStatus.SUCCEEDED.value, payload={"evidence_id": evidence.id})
    elif resolution == "not_executed":
        # This is new user authorization for the same frozen action. Dispatch
        # still checks its descriptor, scope, file version and resource locks.
        status = ActionStatus.CANCELLED if terminal else ActionStatus.READY
        repo.transition_action(action, status.value, payload={"reason": "verified_not_executed"})
    else:
        raise ValueError("invalid_resolution")
    ResourceLockService().release_action(db, task_id=task.id, action_id=action.id)
    repo.append_event(task.id, "action_reconciled_by_user", {
        "action_id": action.id, "resolution": resolution, "observation": observation, "attempt_id": attempt_id,
    }, visibility="conversation", action_id=action.id)
    remaining = db.query(AgentAction.id).filter(
        AgentAction.task_id == task.id,
        AgentAction.status.in_([ActionStatus.DISPATCHED.value, ActionStatus.RUNNING.value, ActionStatus.UNKNOWN.value]),
    ).first()
    if not terminal and not remaining:
        repo.transition_task(task, TaskStatus.QUEUED.value)
        repo.enqueue_run(task, trigger="action_reconciled_by_user")
    repo.checkpoint(task.id, reason="action_reconciled_by_user")
    return {"task_id": task.id, "action_id": action.id, "status": action.status}
