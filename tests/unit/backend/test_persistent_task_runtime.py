"""P0-P8 Persistent Task Runtime 验收测试。"""
from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiive.control.approval_service import TaskApprovalService
from aiive.control.capability_broker import CapabilityBroker
from aiive.control.scope import TaskScope, action_resource_keys, path_within
from aiive.db.models import (
    AgentAction,
    AgentRun,
    AgentTaskEvent,
    AgentTaskWatch,
    ApprovalRequest,
    DesktopNode,
    Event,
    OutboxJob,
    TaskResourceLock,
    TaskEvidence,
    Thread,
)
from aiive.desktop.reconciliation import desktop_reconciliation_service
from aiive.desktop.connection_manager import DesktopDispatchError, desktop_connection_manager
from aiive.selfdev.trusted_core import protected_reason, validate_operations
from aiive.task_runtime.agent_executor import AgentDecision
from aiive.task_runtime.context_assembler import TaskContextAssembler
from aiive.task_runtime.evidence import EvidenceStore
from aiive.task_runtime.repository import (
    TaskRepository,
    reconcile_queued_tasks,
    recover_stale_tasks,
    utcnow,
)
from aiive.task_runtime.resource_locks import ResourceBusyError, ResourceLockService
from aiive.task_runtime.runtime import PersistentTaskRuntime
from aiive.task_runtime.schemas import ActionProposal, ActionStatus, TaskStatus
from aiive.task_runtime.tools import build_main_agent_registry
from aiive.task_runtime.watchers import WatcherService
from aiive.task_runtime.worktree import WorktreeIsolation


class SequenceAgent:
    def __init__(self, *proposals: ActionProposal):
        self.proposals = list(proposals)

    def decide(self, task, capabilities):
        proposal = self.proposals.pop(0)
        return AgentDecision(
            proposal=proposal,
            model="fake-task-model",
            token_usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            input_snapshot={"task_id": task.id, "capabilities": capabilities},
            prompt_refs=["task_runtime.worker_system@test"],
        )


def _thread(db_session) -> Thread:
    thread = Thread(id=str(uuid.uuid4()), title="persistent task test")
    db_session.add(thread)
    db_session.flush()
    return thread


def _task(db_session, thread: Thread, **kwargs):
    return TaskRepository(db_session).create_task(
        thread_id=thread.id,
        goal=kwargs.pop("goal", "完成测试任务"),
        title=kwargs.pop("title", "测试任务"),
        task_type=kwargs.pop("task_type", "general"),
        task_brief=kwargs.pop("task_brief", {"scope": {"allowed_capabilities": ["get_current_time"]}}),
        enqueue=kwargs.pop("enqueue", False),
        **kwargs,
    )


def test_main_agent_only_has_task_meta_tools() -> None:
    registry = build_main_agent_registry()
    assert {item["capability_id"] for item in registry.list_all()} == {
        "list_skills", "delegate_task", "get_task_status", "cancel_task", "send_task_input",
    }
    assert registry.get("desktop_fs_read_text") is None
    assert registry.get("safe_delete") is None
    assert registry.get("promote_slot") is None


def test_create_task_goal_cannot_be_overridden_by_brief(db_session) -> None:
    task = _task(
        db_session,
        _thread(db_session),
        goal="authoritative goal",
        task_brief={"goal": "spoofed goal", "scope": {"allowed_capabilities": ["get_current_time"]}},
    )
    assert task.goal == "authoritative goal"
    assert task.task_brief["goal"] == "authoritative goal"


def test_conversation_task_events_broadcast_only_after_commit(db_session, monkeypatch) -> None:
    from aiive.api.ws_manager import ws_manager

    broadcasts: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        ws_manager,
        "broadcast_to_thread_sync",
        lambda thread_id, event_type, payload: broadcasts.append(
            (thread_id, event_type, payload)
        ),
    )

    outer_task = _task(db_session, _thread(db_session))
    savepoint = db_session.begin_nested()
    _task(db_session, _thread(db_session), title="rolled back nested task")
    assert broadcasts == []
    savepoint.rollback()
    assert broadcasts == []

    committed_savepoint = db_session.begin_nested()
    nested_task = _task(db_session, _thread(db_session), title="committed nested task")
    committed_savepoint.commit()
    assert broadcasts == []

    committed = _task(db_session, _thread(db_session))
    db_session.commit()
    assert [(thread_id, event_type) for thread_id, event_type, _payload in broadcasts] == [
        (outer_task.thread_id, "task_event"),
        (nested_task.thread_id, "task_event"),
        (committed.thread_id, "task_event"),
    ]
    assert broadcasts[0][2]["event_type"] == "task_created"


def test_cancel_task_preserves_pending_action_audit(db_session) -> None:
    repo = TaskRepository(db_session)
    task = _task(db_session, _thread(db_session))
    running_task, run = repo.start_run(task.id, trigger="dispatch")
    action = repo.create_action(
        task=running_task,
        run=run,
        capability_id="get_current_time",
        arguments={},
        descriptor_hash="0" * 64,
        risk_level="low",
        requires_approval=False,
    )
    repo.transition_action(action, ActionStatus.VALIDATED.value)
    repo.transition_action(action, ActionStatus.READY.value)

    cancelled = repo.cancel_task(task.id, reason="test_cancel")
    db_session.refresh(action)

    assert cancelled.status == TaskStatus.CANCELLED.value
    assert action.status == ActionStatus.CANCELLED.value
    assert action.completed_at is not None
    assert action.error_message == "test_cancel"
    event = db_session.query(AgentTaskEvent).filter(
        AgentTaskEvent.action_id == action.id,
        AgentTaskEvent.event_type == "action_cancelled",
    ).one()
    assert event.payload["reason"] == "test_cancel"


def test_simple_readonly_task_runs_to_report(db_session) -> None:
    task = _task(db_session, _thread(db_session))
    agent = SequenceAgent(
        ActionProposal(decision="action", capability_id="get_current_time", summary="读取时间"),
        ActionProposal(decision="finish", summary="时间读取完成", report_status="succeeded"),
    )

    result = PersistentTaskRuntime(db_session, agent_executor=agent).run(task.id)

    db_session.expire_all()
    stored = db_session.get(type(task), task.id)
    assert result["status"] == stored.status == TaskStatus.SUCCEEDED.value
    assert stored.report["summary"] == "时间读取完成"
    actions = db_session.query(AgentAction).filter(AgentAction.task_id == task.id).all()
    assert [item.status for item in actions] == [ActionStatus.SUCCEEDED.value]
    assert db_session.query(TaskEvidence).filter(TaskEvidence.task_id == task.id).count() == 1
    assert db_session.query(AgentRun).filter(AgentRun.task_id == task.id).count() == 1


def test_deterministic_executor_skips_worker_llm_but_keeps_action_audit(db_session) -> None:
    task = _task(
        db_session,
        _thread(db_session),
        task_brief={
            "scope": {"allowed_capabilities": ["get_current_time"]},
            "initial_input": {"deterministic_action": {
                "capability_id": "get_current_time", "arguments": {}, "summary": "读取时间",
            }},
        },
    )

    result = PersistentTaskRuntime(db_session).run(task.id)
    db_session.refresh(task)

    assert result["status"] == TaskStatus.SUCCEEDED.value
    assert task.executor_type == "deterministic"
    assert task.usage["model_calls"] == 0
    assert db_session.query(AgentAction).filter(AgentAction.task_id == task.id).count() == 1


def test_simple_desktop_file_read_stays_inside_task_context(
    db_session, tmp_path, monkeypatch,
) -> None:
    source = tmp_path / "test.txt"
    source.write_text("task-only file content", encoding="utf-8")
    thread = _thread(db_session)
    node = DesktopNode(
        id=str(uuid.uuid4()), name="node", platform="darwin", arch="arm64",
        app_version="1", status="online", lease_expires_at=utcnow() + timedelta(minutes=5),
        capabilities=[{
            "name": "desktop_fs_read_text",
            "description": "读取文本",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        }],
    )
    db_session.add(node)
    db_session.flush()
    monkeypatch.setattr(desktop_connection_manager, "is_connected", lambda candidate: candidate == node.id)
    monkeypatch.setattr(
        desktop_connection_manager,
        "dispatch_action_sync",
        lambda **kwargs: {
            "ok": True,
            "result": {"content": Path(kwargs["params"]["path"]).read_text(encoding="utf-8")},
        },
    )
    task = _task(
        db_session,
        thread,
        target_node_id=node.id,
        task_type="file_operation",
        task_brief={
            "scope": {
                "allowed_capabilities": ["desktop_fs_read_text"],
                "allowed_roots": [str(tmp_path)],
            },
            "initial_input": {"deterministic_action": {
                "capability_id": "desktop_fs_read_text",
                "arguments": {"path": str(source)},
                "summary": "读取 test.txt",
            }},
        },
    )

    result = PersistentTaskRuntime(db_session).run(task.id)
    evidence = db_session.query(TaskEvidence).filter(TaskEvidence.task_id == task.id).one()
    conversation_events = [
        item["event_type"] for item in TaskRepository(db_session).detail(task, include_events=True)["events"]
        if item["visibility"] == "conversation"
    ]

    assert result["status"] == TaskStatus.SUCCEEDED.value
    assert "task-only file content" in evidence.summary
    assert all(not event.startswith("action_") for event in conversation_events)


def test_task_context_never_loads_conversation_history(db_session) -> None:
    thread = _thread(db_session)
    db_session.add(Event(
        trace_id="trace", thread_id=thread.id, event_type="user_message",
        payload={"content": "CONVERSATION_SECRET_MUST_NOT_ENTER_TASK"},
    ))
    task = _task(db_session, thread)
    snapshot = TaskContextAssembler(db_session).snapshot(task, [{"capability_id": "get_current_time"}])

    assert "CONVERSATION_SECRET_MUST_NOT_ENTER_TASK" not in str(snapshot)
    assert snapshot["task"]["goal"] == task.goal


def test_task_context_bounds_large_state_and_exposes_capability_schema(db_session) -> None:
    task = _task(db_session, _thread(db_session))
    task.task_state = {
        "inputs": [{"content": f"input-{index}-" + "x" * 10_000} for index in range(20)],
        "plan": ["p" * 20_000],
    }
    capabilities = CapabilityBroker(db_session).list_capabilities(task)
    snapshot = TaskContextAssembler(db_session).snapshot(task, capabilities)

    assert len(str(snapshot["task"]["state"])) < 40_000
    assert snapshot["task"]["state"]["older_input_count"] == 10
    current_time = next(item for item in capabilities if item["capability_id"] == "get_current_time")
    assert "parameters" in current_time and "input_schema" in current_time


def test_high_risk_action_blocks_for_hours_and_resumes_as_new_run(
    db_session, tmp_path, monkeypatch,
) -> None:
    thread = _thread(db_session)
    target = tmp_path / "old.txt"
    target.write_text("keep until approved")
    task = _task(
        db_session, thread, task_type="file_operation",
        task_brief={"scope": {"allowed_capabilities": ["safe_delete"], "allowed_roots": [str(tmp_path)]}},
    )
    agent = SequenceAgent(ActionProposal(
        decision="action", capability_id="safe_delete",
        arguments={"path": str(target), "scope_id": "test_artifacts", "mode": "trash"},
        effects={"delete": str(target)}, summary="删除文件",
    ))

    result = PersistentTaskRuntime(db_session, agent_executor=agent).run(task.id)
    approval = db_session.query(ApprovalRequest).filter(ApprovalRequest.task_id == task.id).one()
    first_run = db_session.query(AgentRun).filter(AgentRun.task_id == task.id).one()
    assert result["status"] == TaskStatus.BLOCKED_APPROVAL.value
    expires = approval.expires_at.replace(tzinfo=timezone.utc) if approval.expires_at.tzinfo is None else approval.expires_at
    assert expires > utcnow() + timedelta(hours=23)
    assert target.exists()

    approved = TaskApprovalService(db_session).decide(approval.id, "approve")
    db_session.commit()
    db_session.expire_all()
    action = db_session.get(AgentAction, approval.action_id)
    stored = db_session.get(type(task), task.id)
    assert approved["action"] == "succeeded"
    assert action.status == ActionStatus.READY.value
    assert stored.status == TaskStatus.QUEUED.value
    assert first_run.status == "completed"
    assert db_session.query(AgentRun).filter(AgentRun.task_id == task.id).count() == 1

    # 审批只重新入队；下一次 Worker wake 创建新 Run。此处隔离真实删除副作用。
    monkeypatch.setattr(
        CapabilityBroker,
        "dispatch",
        lambda _broker, _task, _run, _action: {"ok": True, "result": {"deleted": True}},
    )
    resumed = PersistentTaskRuntime(
        db_session,
        agent_executor=SequenceAgent(ActionProposal(
            decision="finish", summary="审批后的动作已执行", report_status="succeeded",
        )),
    ).run(task.id, trigger="approval_granted")
    db_session.expire_all()
    runs = db_session.query(AgentRun).filter(AgentRun.task_id == task.id).order_by(AgentRun.run_index).all()
    action = db_session.get(AgentAction, approval.action_id)
    assert resumed["status"] == TaskStatus.SUCCEEDED.value
    assert len(runs) == 2
    assert action.execution_run_id == runs[1].id


def test_toctou_change_invalidates_approved_action(db_session, tmp_path) -> None:
    thread = _thread(db_session)
    target = tmp_path / "changing.txt"
    target.write_text("v1")
    original_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    task = _task(
        db_session, thread, task_type="file_operation",
        task_brief={"scope": {"allowed_capabilities": ["safe_delete"], "allowed_roots": [str(tmp_path)]}},
    )
    PersistentTaskRuntime(db_session, agent_executor=SequenceAgent(ActionProposal(
        decision="action", capability_id="safe_delete",
        arguments={"path": str(target), "scope_id": "test_artifacts", "mode": "trash"},
        preconditions={"exists": True, "sha256": original_hash},
    ))).run(task.id)
    approval = db_session.query(ApprovalRequest).filter(ApprovalRequest.task_id == task.id).one()
    target.write_text("v2")

    result = TaskApprovalService(db_session).decide(approval.id, "approve")
    db_session.commit()
    action = db_session.get(AgentAction, approval.action_id)
    stored = db_session.get(type(task), task.id)
    assert result["action"] == "failed"
    assert action.status == ActionStatus.INVALIDATED.value
    assert stored.status == TaskStatus.QUEUED.value
    assert target.read_text() == "v2"


def test_source_file_metadata_preconditions_are_enforced(db_session, tmp_path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("original", encoding="utf-8")
    stat = source.stat()
    task = _task(db_session, _thread(db_session))
    repo = TaskRepository(db_session)
    _, run = repo.start_run(task.id, trigger="dispatch")
    action = repo.create_action(
        task=task,
        run=run,
        capability_id="desktop_fs_move",
        arguments={"source": str(source), "destination": str(tmp_path / "moved.txt")},
        descriptor_hash="descriptor",
        risk_level="medium",
        requires_approval=False,
        preconditions={
            "exists": True,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "file_id": f"{stat.st_dev}:{stat.st_ino}",
        },
    )
    assert CapabilityBroker.validate_preconditions(action) == []
    source.write_text("changed", encoding="utf-8")
    assert CapabilityBroker.validate_preconditions(action)


def test_expired_task_approval_is_requeued_by_scheduler_service(db_session, tmp_path) -> None:
    target = tmp_path / "expired.txt"
    target.write_text("keep", encoding="utf-8")
    task = _task(
        db_session,
        _thread(db_session),
        task_type="file_operation",
        task_brief={"scope": {"allowed_capabilities": ["safe_delete"], "allowed_roots": [str(tmp_path)]}},
    )
    PersistentTaskRuntime(db_session, agent_executor=SequenceAgent(ActionProposal(
        decision="action",
        capability_id="safe_delete",
        arguments={"path": str(target), "scope_id": "test_artifacts", "mode": "trash"},
    ))).run(task.id)
    approval = db_session.query(ApprovalRequest).filter(ApprovalRequest.task_id == task.id).one()
    approval.expires_at = utcnow() - timedelta(seconds=1)
    db_session.flush()

    expired = TaskApprovalService(db_session).expire_due()

    assert expired == 1
    assert task.status == TaskStatus.QUEUED.value
    assert db_session.get(AgentAction, approval.action_id).status == ActionStatus.INVALIDATED.value
    assert approval.status == "failed"


def test_task_approval_rejects_tampered_frozen_arguments(db_session, tmp_path) -> None:
    target = tmp_path / "approved.txt"
    target.write_text("approved")
    other = tmp_path / "not-approved.txt"
    other.write_text("do not touch")
    task = _task(
        db_session, _thread(db_session), task_type="file_operation",
        task_brief={"scope": {"allowed_capabilities": ["safe_delete"], "allowed_roots": [str(tmp_path)]}},
    )
    PersistentTaskRuntime(db_session, agent_executor=SequenceAgent(ActionProposal(
        decision="action", capability_id="safe_delete",
        arguments={"path": str(target), "scope_id": "test_artifacts", "mode": "trash"},
    ))).run(task.id)
    approval = db_session.query(ApprovalRequest).filter(ApprovalRequest.task_id == task.id).one()
    approval.tool_args = {"path": str(other), "scope_id": "test_artifacts", "mode": "trash"}
    db_session.flush()

    result = TaskApprovalService(db_session).decide(approval.id, "approve")

    assert result["action"] == "failed"
    assert db_session.get(AgentAction, approval.action_id).status == ActionStatus.INVALIDATED.value
    assert target.exists() and other.exists()


def test_server_restart_recovers_run_without_replaying_unknown_action(db_session) -> None:
    task = _task(db_session, _thread(db_session))
    repo = TaskRepository(db_session)
    _, run = repo.start_run(task.id, trigger="dispatch")
    run.lease_expires_at = utcnow() - timedelta(seconds=1)
    db_session.flush()

    recovered = recover_stale_tasks(db_session)

    assert recovered == 1
    assert run.status == "interrupted"
    assert task.status == TaskStatus.QUEUED.value
    assert db_session.query(AgentRun).filter(AgentRun.task_id == task.id).count() == 1


def test_queued_task_without_live_outbox_job_is_reconciled(db_session) -> None:
    task = _task(db_session, _thread(db_session), enqueue=False)

    enqueued = reconcile_queued_tasks(db_session)

    assert enqueued == 1
    jobs = db_session.query(OutboxJob).filter(OutboxJob.job_type == "agent_task_run").all()
    assert any(job.operation_id.startswith(f"agent_task_run:{task.id}:") for job in jobs)


def test_desktop_unknown_reconciles_from_node_journal(db_session) -> None:
    thread = _thread(db_session)
    node = DesktopNode(
        id=str(uuid.uuid4()), name="node", platform="darwin", arch="arm64",
        app_version="1", capabilities=[], status="online",
    )
    db_session.add(node)
    task = _task(db_session, thread, target_node_id=node.id)
    repo = TaskRepository(db_session)
    _, run = repo.start_run(task.id, trigger="dispatch")
    action = repo.create_action(
        task=task, run=run, capability_id="desktop_fs_read_text", arguments={"path": "/tmp/a"},
        descriptor_hash="descriptor", risk_level="low", requires_approval=False,
        target_node_id=node.id,
    )
    repo.transition_action(action, ActionStatus.VALIDATED.value)
    repo.transition_action(action, ActionStatus.READY.value)
    repo.transition_action(action, ActionStatus.DISPATCHED.value)
    repo.transition_action(action, ActionStatus.UNKNOWN.value)
    repo.transition_task(task, TaskStatus.RECONCILING.value)

    applied = desktop_reconciliation_service.apply_entry(db_session, node.id, {
        "action_id": action.id,
        "idempotency_key": action.idempotency_key,
        "arguments_hash": action.arguments_hash,
        "status": "committed",
        "result": {"content": "hello"},
        "result_hash": hashlib.sha256(b'{"content":"hello"}').hexdigest(),
    })

    assert applied is True
    assert action.status == ActionStatus.SUCCEEDED.value
    assert task.status == TaskStatus.QUEUED.value


def test_unknown_desktop_action_retains_lock_until_journal_reconciliation(
    db_session, tmp_path, monkeypatch,
) -> None:
    thread = _thread(db_session)
    node = DesktopNode(
        id=str(uuid.uuid4()), name="node", platform="darwin", arch="arm64",
        app_version="1", status="online", lease_expires_at=utcnow() + timedelta(minutes=5),
        capabilities=[{
            "name": "desktop_fs_write_text",
            "description": "write",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        }],
    )
    db_session.add(node)
    monkeypatch.setattr(desktop_connection_manager, "is_connected", lambda candidate: candidate == node.id)
    monkeypatch.setattr(
        desktop_connection_manager,
        "dispatch_action_sync",
        lambda **_kwargs: (_ for _ in ()).throw(DesktopDispatchError("desktop_operation_timeout")),
    )
    target = tmp_path / "locked.txt"
    task = _task(
        db_session,
        thread,
        target_node_id=node.id,
        task_type="file_operation",
        task_brief={
            "scope": {
                "allowed_capabilities": ["desktop_fs_write_text"],
                "allowed_roots": [str(tmp_path)],
            },
            "initial_input": {"deterministic_action": {
                "capability_id": "desktop_fs_write_text",
                "arguments": {"path": str(target), "content": "value"},
            }},
        },
    )

    result = PersistentTaskRuntime(db_session).run(task.id)
    action = db_session.query(AgentAction).filter(AgentAction.task_id == task.id).one()

    assert result["status"] == TaskStatus.RECONCILING.value
    assert action.status == ActionStatus.UNKNOWN.value
    assert db_session.query(TaskResourceLock).filter(TaskResourceLock.action_id == action.id).count() == 1

    reconciled_result = {"path": str(target), "bytes_written": 5}
    assert desktop_reconciliation_service.apply_entry(db_session, node.id, {
        "action_id": action.id,
        "idempotency_key": action.idempotency_key,
        "arguments_hash": action.arguments_hash,
        "status": "committed",
        "result": reconciled_result,
        "result_hash": hashlib.sha256(
            json.dumps(reconciled_result, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }) is True
    assert db_session.query(TaskResourceLock).filter(TaskResourceLock.action_id == action.id).count() == 0


def test_user_input_does_not_bypass_unknown_action_reconciliation(db_session) -> None:
    task = _task(db_session, _thread(db_session))
    repo = TaskRepository(db_session)
    repo.transition_task(task, TaskStatus.DISPATCHING.value)
    repo.transition_task(task, TaskStatus.RUNNING.value)
    repo.transition_task(task, TaskStatus.RECONCILING.value)

    repo.send_input(task.id, "additional context only")

    assert task.status == TaskStatus.RECONCILING.value
    assert not db_session.query(OutboxJob).filter(
        OutboxJob.operation_id.like(f"agent_task_run:{task.id}:%user_input"),
    ).count()


def test_desktop_missing_journal_entry_only_retries_unacked_unknown_action(db_session) -> None:
    thread = _thread(db_session)
    node = DesktopNode(
        id=str(uuid.uuid4()), name="node", platform="darwin", arch="arm64",
        app_version="1", capabilities=[], status="online",
    )
    db_session.add(node)
    task = _task(db_session, thread, target_node_id=node.id)
    repo = TaskRepository(db_session)
    _, run = repo.start_run(task.id, trigger="dispatch")
    action = repo.create_action(
        task=task, run=run, capability_id="desktop_fs_move",
        arguments={"source": "/tmp/a", "destination": "/tmp/b"},
        descriptor_hash="descriptor", risk_level="medium", requires_approval=False,
        target_node_id=node.id,
    )
    repo.transition_action(action, ActionStatus.VALIDATED.value)
    repo.transition_action(action, ActionStatus.READY.value)
    repo.transition_action(action, ActionStatus.DISPATCHED.value)
    repo.transition_action(action, ActionStatus.UNKNOWN.value)
    repo.transition_task(task, TaskStatus.RECONCILING.value)
    repo.finish_run(run, summary={"blocked": "reconciliation"})

    applied = desktop_reconciliation_service.apply_missing(db_session, node.id, action.id)

    assert applied is True
    assert action.status == ActionStatus.READY.value
    assert task.status == TaskStatus.QUEUED.value


def test_large_stdout_becomes_object_evidence_with_range_reads(db_session) -> None:
    task = _task(db_session, _thread(db_session))
    repo = TaskRepository(db_session)
    _, run = repo.start_run(task.id, trigger="dispatch")
    action = repo.create_action(
        task=task, run=run, capability_id="get_current_time", arguments={},
        descriptor_hash="descriptor", risk_level="low", requires_approval=False,
    )
    payload = {"ok": True, "result": {"stdout": "x" * 20_000}}

    evidence = EvidenceStore().capture_result(
        db_session, task_id=task.id, run_id=run.id, action=action, result=payload,
    )
    chunk, truncated = EvidenceStore.read(db_session, evidence, 100, 256)

    assert evidence.inline_payload is None
    assert evidence.object_key
    assert len(chunk) == 256
    assert truncated is True


def test_self_improvement_promotion_accepts_object_stored_test_evidence(db_session) -> None:
    task = _task(
        db_session,
        _thread(db_session),
        task_type="self_improvement",
        task_brief={"scope": {"allowed_capabilities": ["promote_slot"]}},
    )
    broker = CapabilityBroker(db_session)
    assert broker.prepare(task, "promote_slot", {}).decision.outcome == "block"

    repo = TaskRepository(db_session)
    _, run = repo.start_run(task.id, trigger="evaluation")
    action = repo.create_action(
        task=task, run=run, capability_id="promote_slot", arguments={},
        descriptor_hash="evaluation", risk_level="high", requires_approval=True,
    )
    evidence = EvidenceStore().capture_result(
        db_session,
        task_id=task.id,
        run_id=run.id,
        action=action,
        result={"targeted_test_report": {"ok": True}, "log": "x" * 10_000},
        kind="isolated_evaluation",
    )

    assert evidence.inline_payload is None
    prepared = broker.prepare(task, "promote_slot", {})
    assert prepared.decision.outcome == "approval"


def test_file_watcher_emits_event_and_wakes_task(db_session, tmp_path) -> None:
    thread = _thread(db_session)
    path = tmp_path / "watched.txt"
    path.write_text("before")
    task = _task(
        db_session, thread,
        task_brief={"scope": {"allowed_capabilities": ["get_current_time"], "allowed_roots": [str(tmp_path)]}},
    )
    repo = TaskRepository(db_session)
    repo.transition_task(task, TaskStatus.DISPATCHING.value)
    repo.transition_task(task, TaskStatus.RUNNING.value)
    repo.transition_task(task, TaskStatus.BLOCKED_USER.value)
    watch = WatcherService(db_session).create_from_proposal(task, {
        "watch_type": "file", "config": {"path": str(path), "condition": "changed"},
    })
    path.write_text("after-change")

    triggered = WatcherService(db_session).poll_due(now=utcnow() + timedelta(seconds=1))

    assert triggered == 1
    assert watch.status == "completed"
    assert task.status == TaskStatus.QUEUED.value


def test_node_online_event_wakes_blocked_target_task(db_session) -> None:
    thread = _thread(db_session)
    node = DesktopNode(
        id=str(uuid.uuid4()), name="node", platform="darwin", arch="arm64",
        app_version="1", capabilities=[], status="online",
    )
    db_session.add(node)
    task = _task(db_session, thread, target_node_id=node.id)
    repo = TaskRepository(db_session)
    repo.transition_task(task, TaskStatus.DISPATCHING.value)
    repo.transition_task(task, TaskStatus.RUNNING.value)
    repo.transition_task(task, TaskStatus.BLOCKED_NODE.value)

    awakened = WatcherService(db_session).wake_node_tasks(node.id)

    assert awakened == 1
    assert task.status == TaskStatus.QUEUED.value


def test_node_watch_requires_an_explicit_or_task_target_node(db_session) -> None:
    task = _task(db_session, _thread(db_session))
    try:
        WatcherService(db_session).create_from_proposal(task, {
            "watch_type": "node", "config": {},
        })
        raise AssertionError("node watch unexpectedly accepted no node identity")
    except ValueError as error:
        assert str(error) == "node_watch_requires_node_id"


def test_resource_lock_and_scope_guards(db_session, tmp_path) -> None:
    thread = _thread(db_session)
    task1 = _task(db_session, thread)
    task2 = _task(db_session, thread)
    locks = ResourceLockService()
    key = f"server:path:{tmp_path}"
    grant = locks.acquire(db_session, task_id=task1.id, action_id="a1", resource_keys=[key])
    try:
        locks.acquire(db_session, task_id=task2.id, action_id="a2", resource_keys=[key])
        raise AssertionError("second task unexpectedly acquired active resource")
    except ResourceBusyError:
        pass
    assert locks.release(db_session, grant) == 1
    assert path_within(str(tmp_path / "child"), str(tmp_path))
    assert not path_within("C:/allowed/../outside", "C:/allowed")
    scope = TaskScope(frozenset({"desktop_fs_read_text"}), (str(tmp_path),), frozenset())
    assert scope.validate_arguments("desktop_fs_read_text", {"path": str(tmp_path / "ok")}) == []
    assert scope.validate_arguments("desktop_fs_read_text", {"path": "/outside"})
    assert action_resource_keys("desktop_gui_click", {}, "node-1") == ["node:node-1:gui:foreground"]
    assert action_resource_keys("desktop_fs_read_text", {"path": str(tmp_path)}, "node-1") == []
    assert action_resource_keys(
        "desktop_fs_write_text", {"path": str(tmp_path)}, "node-1", mutates_resources=True,
    ) == [f"node:node-1:path:{tmp_path}"]


def test_complex_task_keeps_multiple_actions_in_task_timeline(db_session) -> None:
    task = _task(db_session, _thread(db_session), task_type="invoice_organization")
    agent = SequenceAgent(
        ActionProposal(decision="action", capability_id="get_current_time", summary="扫描阶段"),
        ActionProposal(decision="action", capability_id="get_current_time", summary="验证阶段"),
        ActionProposal(decision="finish", summary="多阶段任务完成", report_status="succeeded"),
    )

    result = PersistentTaskRuntime(db_session, agent_executor=agent).run(task.id)
    detail = TaskRepository(db_session).detail(task, include_events=True)

    assert result["status"] == TaskStatus.SUCCEEDED.value
    assert [item["sequence"] for item in detail["actions"]] == [1, 2]
    assert all(item["status"] == ActionStatus.SUCCEEDED.value for item in detail["actions"])
    assert len(detail["runs"]) == 1
    assert any(item["event_type"] == "task_completed" for item in detail["events"])


def test_action_budget_stops_new_action_without_losing_completed_evidence(db_session) -> None:
    task = _task(db_session, _thread(db_session), budgets={"max_actions": 1})
    agent = SequenceAgent(
        ActionProposal(decision="action", capability_id="get_current_time", summary="first"),
        ActionProposal(decision="action", capability_id="get_current_time", summary="over budget"),
    )

    result = PersistentTaskRuntime(db_session, agent_executor=agent).run(task.id)
    actions = db_session.query(AgentAction).filter(AgentAction.task_id == task.id).all()

    assert result["status"] == TaskStatus.PARTIAL.value
    assert len(actions) == 1 and actions[0].status == ActionStatus.SUCCEEDED.value
    assert result["report"]["unresolved"] == ["action_budget_exhausted"]


def test_self_improvement_trusted_core_is_immutable() -> None:
    assert protected_reason("backend/aiive/task_runtime/runtime.py")
    assert protected_reason("../backend/aiive/main.py")
    assert protected_reason("../frontend/src/App.tsx") == "invalid_or_parent_traversal_path"
    assert protected_reason("/tmp/absolute.py") == "invalid_or_parent_traversal_path"
    assert validate_operations([
        {"operation": "modify_file", "target_file": "backend/aiive/control/task_policy.py"},
    ])
    assert validate_operations([
        {"operation": "modify_file", "target_file": "frontend/src/pages/TaskCenterPage.tsx"},
    ]) == []


def test_explicit_git_worktree_isolation_rewrites_task_scope(db_session, tmp_path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    (repository / "README.md").write_text("baseline", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=AIive Test", "-c", "user.email=test@aiive.local", "commit", "-m", "baseline"],
        cwd=repository, check=True, capture_output=True,
    )
    task = _task(
        db_session,
        _thread(db_session),
        task_type="development",
        task_brief={"scope": {
            "allowed_capabilities": ["get_current_time"],
            "allowed_roots": [str(repository)],
            "isolation_mode": "git_worktree",
        }},
    )

    result = PersistentTaskRuntime(
        db_session,
        agent_executor=SequenceAgent(ActionProposal(
            decision="finish", summary="隔离任务完成", report_status="succeeded",
        )),
    ).run(task.id)
    db_session.refresh(task)
    workspace = task.task_state["worktree_path"]

    assert result["status"] == TaskStatus.SUCCEEDED.value
    assert task.task_brief["scope"]["allowed_roots"] == [workspace]
    assert (tmp_path / "repository" / ".data" / "task-worktrees" / task.id).resolve() == Path(workspace).resolve()
    assert (Path(workspace) / "README.md").exists()
    isolation = WorktreeIsolation(repository)
    isolation.remove(task.id)

    # 删除 worktree 不会删除分支；再次创建必须复用原分支，不能用 -b 重建。
    recreated = isolation.create(task.id)
    assert recreated.resolve() == Path(workspace).resolve()
    assert (recreated / "README.md").exists()
    isolation.remove(task.id)
