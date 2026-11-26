"""取消后的 Worker 崩溃恢复：Task 保持 cancelled，只收敛 Run 与 Action。"""
from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

from aiive.db.models import (
    AgentAction,
    AgentRun,
    AgentTaskEvent,
    DesktopNode,
    OutboxJob,
    Thread,
)
from aiive.desktop.reconciliation import desktop_reconciliation_service
from aiive.task_runtime.repository import TaskRepository, recover_stale_tasks, utcnow
from aiive.task_runtime.schemas import ActionStatus, RunStatus, TaskStatus


def _thread(db_session) -> Thread:
    thread = Thread(id=str(uuid.uuid4()), title="cancelled recovery test")
    db_session.add(thread)
    db_session.flush()
    return thread


def _cancelled_task_with_dispatched_action(db_session, *, lease_seconds: float = -30, node_id: str | None = None):
    """复现：Task=running、Run=running、Action=dispatched 后用户取消并崩溃。"""
    task = TaskRepository(db_session).create_task(
        thread_id=_thread(db_session).id,
        goal="取消后崩溃恢复",
        title="取消恢复",
        task_type="general",
        task_brief={"scope": {"allowed_capabilities": ["get_runtime_identity"]}},
        enqueue=False,
    )
    repo = TaskRepository(db_session)
    task, run = repo.start_run(task.id, trigger="dispatch")
    action = repo.create_action(
        task=task,
        run=run,
        capability_id="get_runtime_identity",
        arguments={},
        descriptor_hash="descriptor",
        risk_level="low",
        requires_approval=False,
        target_node_id=node_id,
    )
    repo.transition_action(action, ActionStatus.VALIDATED.value)
    repo.transition_action(action, ActionStatus.READY.value)
    # 与真实派发一致：派发时写入执行身份与 fencing token。
    action.execution_run_id = run.id
    action.execution_token = str(uuid.uuid4())
    repo.transition_action(action, ActionStatus.DISPATCHED.value)
    repo.cancel_task(task.id, reason="user_cancelled")
    db_session.flush()
    # cancel_task 用批量 UPDATE 设置 cancel_requested_at（synchronize_session=False）。
    db_session.refresh(action)
    run.lease_expires_at = utcnow() + timedelta(seconds=lease_seconds)
    db_session.flush()
    return task, run, action


def _event_types(db_session, task_id: str) -> list[str]:
    return [
        row[0] for row in db_session.query(AgentTaskEvent.event_type).filter(
            AgentTaskEvent.task_id == task_id,
        ).order_by(AgentTaskEvent.sequence.asc())
    ]


def test_expired_cancelled_execution_is_recovered_without_restart(db_session) -> None:
    task, run, action = _cancelled_task_with_dispatched_action(db_session)
    requested_at = action.cancel_requested_at
    token = action.execution_token
    assert requested_at is not None and token


    recovered = recover_stale_tasks(db_session)

    db_session.expire_all()
    assert recovered == 1
    assert db_session.get(type(task), task.id).status == TaskStatus.CANCELLED.value
    assert db_session.get(AgentRun, run.id).status == RunStatus.INTERRUPTED.value
    stored_action = db_session.get(AgentAction, action.id)
    assert stored_action.status == ActionStatus.UNKNOWN.value
    # 取消证据与 Action 参数、执行身份必须原样保留。
    assert stored_action.cancel_requested_at == requested_at
    assert stored_action.arguments == {}
    assert stored_action.execution_run_id == run.id
    assert stored_action.execution_token == token
    events = db_session.query(AgentTaskEvent).filter(
        AgentTaskEvent.task_id == task.id,
        AgentTaskEvent.event_type == "cancelled_execution_recovered",
    ).all()
    assert len(events) == 1
    assert events[0].payload["reason"] == "cancelled_worker_lease_expired"
    assert events[0].payload["action_ids"] == [action.id]
    # 不重放：不新建 Run，也不新建执行 Job。
    assert db_session.query(AgentRun).filter(AgentRun.task_id == task.id).count() == 1
    assert db_session.query(OutboxJob).filter(
        OutboxJob.job_type == "agent_task_run",
    ).count() == 0


def test_repeated_recovery_is_idempotent(db_session) -> None:
    task, run, action = _cancelled_task_with_dispatched_action(db_session)

    assert recover_stale_tasks(db_session) == 1
    first_events = _event_types(db_session, task.id)

    assert recover_stale_tasks(db_session) == 0

    db_session.expire_all()
    assert _event_types(db_session, task.id) == first_events
    assert db_session.get(type(task), task.id).status == TaskStatus.CANCELLED.value
    assert db_session.get(AgentRun, run.id).status == RunStatus.INTERRUPTED.value
    assert db_session.get(AgentAction, action.id).status == ActionStatus.UNKNOWN.value


def test_live_worker_lease_is_not_recovered(db_session) -> None:
    task, run, action = _cancelled_task_with_dispatched_action(db_session, lease_seconds=300)

    assert recover_stale_tasks(db_session) == 0

    db_session.expire_all()
    assert db_session.get(AgentRun, run.id).status == RunStatus.RUNNING.value
    assert db_session.get(AgentAction, action.id).status == ActionStatus.DISPATCHED.value
    assert "cancelled_execution_recovered" not in _event_types(db_session, task.id)


def test_existing_successful_action_is_preserved(db_session) -> None:
    task, run, action = _cancelled_task_with_dispatched_action(db_session)
    repo = TaskRepository(db_session)
    succeeded = repo.create_action(
        task=task,
        run=run,
        capability_id="get_runtime_identity",
        arguments={"note": "already done"},
        descriptor_hash="descriptor",
        risk_level="low",
        requires_approval=False,
    )
    repo.transition_action(succeeded, ActionStatus.VALIDATED.value)
    repo.transition_action(succeeded, ActionStatus.READY.value)
    repo.transition_action(succeeded, ActionStatus.DISPATCHED.value)
    repo.transition_action(succeeded, ActionStatus.SUCCEEDED.value)

    recover_stale_tasks(db_session)

    db_session.expire_all()
    stored = db_session.get(AgentAction, succeeded.id)
    assert stored.status == ActionStatus.SUCCEEDED.value
    assert stored.arguments == {"note": "already done"}


def test_desktop_journal_reconciles_after_recovery_without_reactivating_task(db_session) -> None:
    node = DesktopNode(
        id=str(uuid.uuid4()), name="node", platform="darwin", arch="arm64",
        app_version="1", capabilities=[], status="online",
    )
    db_session.add(node)
    db_session.flush()
    task, run, action = _cancelled_task_with_dispatched_action(db_session, node_id=node.id)

    assert recover_stale_tasks(db_session) == 1
    db_session.expire_all()
    assert db_session.get(AgentAction, action.id).status == ActionStatus.UNKNOWN.value

    applied = desktop_reconciliation_service.apply_entry(db_session, node.id, {
        "action_id": action.id,
        "idempotency_key": action.idempotency_key,
        "arguments_hash": action.arguments_hash,
        "status": "committed",
        "result": {"content": "done"},
        "result_hash": hashlib.sha256(b'{"content":"done"}').hexdigest(),
    })

    db_session.expire_all()
    stored_action = db_session.get(AgentAction, action.id)
    assert applied is True
    # 真实成功回执覆盖 UNKNOWN，但任务不会因此复活。
    assert stored_action.status == ActionStatus.SUCCEEDED.value
    assert db_session.get(type(task), task.id).status == TaskStatus.CANCELLED.value
    assert db_session.get(AgentRun, run.id).status == RunStatus.INTERRUPTED.value
    assert db_session.query(OutboxJob).filter(OutboxJob.job_type == "agent_task_run").count() == 0
