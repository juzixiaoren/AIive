"""Persistent Task 的事务仓储、事件分配、checkpoint 与恢复入队。"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import event as sa_event, func
from sqlalchemy.orm import Session

from aiive.db.models import (
    AgentAction,
    AgentRun,
    AgentTask,
    AgentTaskCheckpoint,
    AgentTaskEvent,
    ApprovalRequest,
    OutboxJob,
    TaskArtifact,
    TaskEvidence,
    Thread,
)
from aiive.task_runtime.schemas import ActionStatus, RunStatus, TaskBrief, TaskBudget, TaskStatus
from aiive.task_runtime.state_machine import ensure_transition


logger = logging.getLogger(__name__)
_TASK_BROADCASTS_KEY = "aiive_task_broadcasts_after_commit"
_TASK_BROADCAST_WATERMARKS_KEY = "aiive_task_broadcast_watermarks"


@sa_event.listens_for(Session, "after_transaction_create")
def _remember_nested_broadcast_watermark(session: Session, transaction: Any) -> None:
    """记录 SAVEPOINT 进入时的队列长度，以便只撤销内层产生的广播。"""
    if transaction.nested:
        pending = session.info.get(_TASK_BROADCASTS_KEY, [])
        session.info.setdefault(_TASK_BROADCAST_WATERMARKS_KEY, {})[id(transaction)] = len(pending)


@sa_event.listens_for(Session, "after_commit")
def _broadcast_task_events_after_commit(session: Session) -> None:
    """只广播已经成为数据库事实的高层 Task 事件，避免回滚后出现幽灵消息。"""
    # SQLAlchemy 对 SAVEPOINT release 也触发 after_commit；此时外层事务仍可回滚。
    if session.in_nested_transaction():
        return
    pending = session.info.pop(_TASK_BROADCASTS_KEY, [])
    session.info.pop(_TASK_BROADCAST_WATERMARKS_KEY, None)
    if not pending:
        return
    try:
        from aiive.api.ws_manager import ws_manager

        for thread_id, payload in pending:
            ws_manager.broadcast_to_thread_sync(thread_id, "task_event", payload)
    except Exception:
        logger.exception("commit 后广播 Task 事件失败")


@sa_event.listens_for(Session, "after_rollback")
def _discard_rolled_back_task_broadcasts(session: Session) -> None:
    nested = session.get_nested_transaction()
    if nested is None:
        session.info.pop(_TASK_BROADCASTS_KEY, None)
        session.info.pop(_TASK_BROADCAST_WATERMARKS_KEY, None)
        return
    watermarks = session.info.get(_TASK_BROADCAST_WATERMARKS_KEY, {})
    watermark = watermarks.get(id(nested))
    pending = session.info.get(_TASK_BROADCASTS_KEY)
    if watermark is not None and isinstance(pending, list):
        del pending[watermark:]


@sa_event.listens_for(Session, "after_transaction_end")
def _discard_nested_broadcast_watermark(session: Session, transaction: Any) -> None:
    if not transaction.nested:
        return
    watermarks = session.info.get(_TASK_BROADCAST_WATERMARKS_KEY)
    if isinstance(watermarks, dict):
        watermarks.pop(id(transaction), None)
        if not watermarks:
            session.info.pop(_TASK_BROADCAST_WATERMARKS_KEY, None)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


TERMINAL_TASK_STATES = {
    TaskStatus.SUCCEEDED.value,
    TaskStatus.PARTIAL.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELLED.value,
}


class TaskRepository:
    """所有 Task 聚合写入均经由此类；调用方负责 commit/rollback。"""

    def __init__(self, db: Session):
        self.db = db

    def create_task(
        self,
        *,
        thread_id: str,
        goal: str,
        title: str | None = None,
        task_type: str = "general",
        source_turn_record_id: str | None = None,
        target_node_id: str | None = None,
        task_brief: dict[str, Any] | None = None,
        budgets: dict[str, Any] | None = None,
        priority: int = 50,
        enqueue: bool = True,
    ) -> AgentTask:
        if self.db.get(Thread, thread_id) is None:
            raise ValueError("thread_not_found")
        # ``goal`` 是 API/Meta Tool 的权威用户输入，不能被 task_brief 内同名字段覆盖。
        brief = TaskBrief.model_validate({**(task_brief or {}), "goal": goal})
        budget = TaskBudget.model_validate(budgets or {})
        from aiive.task_runtime.router import select_executor_type

        task = AgentTask(
            thread_id=thread_id,
            source_turn_record_id=source_turn_record_id,
            task_type=task_type.strip() or "general",
            title=(title or goal.strip().splitlines()[0])[:255],
            goal=goal.strip(),
            status=TaskStatus.QUEUED.value,
            executor_type=select_executor_type(brief.model_dump(mode="json")),
            target_node_id=target_node_id,
            task_brief=brief.model_dump(mode="json"),
            task_state={"plan": [], "inputs": [], "last_decision": None},
            budgets=budget.model_dump(mode="json"),
            usage={
                "runs": 0,
                "model_calls": 0,
                "actions": 0,
                "tool_failures": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0.0,
            },
            priority=max(0, min(100, priority)),
        )
        self.db.add(task)
        self.db.flush()
        self.append_event(
            task.id,
            "task_created",
            {"title": task.title, "goal": task.goal, "task_type": task.task_type},
            visibility="conversation",
            dedupe_key="task_created",
        )
        self.checkpoint(task.id, reason="created")
        if enqueue:
            self.enqueue_run(task, trigger="dispatch")
        return task

    def get_task(self, task_id: str, *, for_update: bool = False) -> AgentTask | None:
        query = self.db.query(AgentTask).filter(AgentTask.id == task_id)
        if for_update:
            query = query.with_for_update()
        return query.one_or_none()

    def transition_task(
        self,
        task: AgentTask,
        status: str,
        *,
        error: str | None = None,
        report: dict[str, Any] | None = None,
        event_type: str | None = None,
        event_payload: dict[str, Any] | None = None,
        visibility: str = "task",
    ) -> AgentTask:
        ensure_transition("task", task.status, status)
        task.status = status
        task.version += 1
        task.updated_at = utcnow()
        if error is not None:
            task.last_error = error[:8000]
        if report is not None:
            task.report = report
        if status in TERMINAL_TASK_STATES:
            task.completed_at = utcnow()
            task.wake_at = None
        self.db.flush()
        if event_type:
            self.append_event(
                task.id,
                event_type,
                event_payload or {"status": status},
                visibility=visibility,
            )
        return task

    def append_event(
        self,
        task_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        visibility: str = "task",
        run_id: str | None = None,
        action_id: str | None = None,
        dedupe_key: str | None = None,
    ) -> AgentTaskEvent:
        # 锁 Task 行即可串行化 sequence 分配；SQLite 写事务也会序列化。
        self.db.query(AgentTask).filter(AgentTask.id == task_id).with_for_update().one()
        if dedupe_key:
            existing = self.db.query(AgentTaskEvent).filter(
                AgentTaskEvent.task_id == task_id,
                AgentTaskEvent.dedupe_key == dedupe_key,
            ).one_or_none()
            if existing is not None:
                return existing
        sequence = int(self.db.query(func.max(AgentTaskEvent.sequence)).filter(
            AgentTaskEvent.task_id == task_id,
        ).scalar() or 0) + 1
        event = AgentTaskEvent(
            task_id=task_id,
            sequence=sequence,
            event_type=event_type,
            visibility=visibility,
            run_id=run_id,
            action_id=action_id,
            payload=payload or {},
            dedupe_key=dedupe_key,
        )
        self.db.add(event)
        self.db.flush()
        if visibility == "conversation":
            task = self.db.get(AgentTask, task_id)
            if task is not None:
                self.db.info.setdefault(_TASK_BROADCASTS_KEY, []).append((task.thread_id, {
                    "task_id": task.id,
                    "title": task.title,
                    "status": task.status,
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "payload": event.payload,
                }))
        return event

    def checkpoint(
        self,
        task_id: str,
        *,
        reason: str,
        plan: dict[str, Any] | list[Any] | None = None,
        pending_action_refs: list[str] | None = None,
    ) -> AgentTaskCheckpoint:
        task = self.get_task(task_id, for_update=True)
        if task is None:
            raise ValueError("task_not_found")
        version = int(self.db.query(func.max(AgentTaskCheckpoint.version)).filter(
            AgentTaskCheckpoint.task_id == task_id,
        ).scalar() or 0) + 1
        event_sequence = int(self.db.query(func.max(AgentTaskEvent.sequence)).filter(
            AgentTaskEvent.task_id == task_id,
        ).scalar() or 0)
        evidence_refs = [row[0] for row in self.db.query(TaskEvidence.id).filter(
            TaskEvidence.task_id == task_id,
        ).order_by(TaskEvidence.created_at.asc()).all()]
        snapshot = {
            "task_status": task.status,
            "task_state": task.task_state or {},
            "usage": task.usage or {},
            "current_run_id": task.current_run_id,
            "event_sequence": event_sequence,
        }
        saved_plan = plan if plan is not None else (task.task_state or {}).get("plan", [])
        if not isinstance(saved_plan, (dict, list)):
            saved_plan = []
        checkpoint = AgentTaskCheckpoint(
            task_id=task_id,
            version=version,
            event_sequence=event_sequence,
            state=snapshot,
            plan=saved_plan,
            pending_action_refs=pending_action_refs or [],
            evidence_refs=evidence_refs,
            reason=reason[:128],
            snapshot_hash=sha256_json(snapshot),
        )
        self.db.add(checkpoint)
        self.db.flush()
        return checkpoint

    def enqueue_run(self, task: AgentTask, *, trigger: str, available_at: datetime | None = None) -> OutboxJob:
        operation_id = f"agent_task_run:{task.id}:v{task.version}:{trigger}"
        existing = self.db.query(OutboxJob).filter(OutboxJob.operation_id == operation_id).one_or_none()
        if existing is not None:
            return existing
        job = OutboxJob(
            operation_id=operation_id,
            job_type="agent_task_run",
            status="pending",
            payload={"task_id": task.id, "trigger": trigger},
            trace_id=task.id,
            schema_version=1,
            max_retries=5,
            available_at=available_at,
        )
        self.db.add(job)
        self.db.flush()
        return job

    def start_run(self, task_id: str, *, trigger: str, lease_seconds: int = 120) -> tuple[AgentTask, AgentRun]:
        task = self.get_task(task_id, for_update=True)
        if task is None:
            raise ValueError("task_not_found")
        if task.status in TERMINAL_TASK_STATES:
            raise ValueError("task_terminal")
        if task.status != TaskStatus.QUEUED.value:
            raise ValueError(f"task_not_runnable:{task.status}")
        self.transition_task(task, TaskStatus.DISPATCHING.value)
        checkpoint = self.latest_checkpoint(task.id)
        run_index = int(self.db.query(func.max(AgentRun.run_index)).filter(
            AgentRun.task_id == task.id,
        ).scalar() or 0) + 1
        run = AgentRun(
            task_id=task.id,
            run_index=run_index,
            trigger=trigger,
            status=RunStatus.CREATED.value,
            execution_id=str(uuid.uuid4()),
            lease_expires_at=utcnow() + timedelta(seconds=lease_seconds),
            input_snapshot={
                "task_id": task.id,
                "task_version": task.version,
                "checkpoint_id": checkpoint.id if checkpoint else None,
                "usage": task.usage or {},
            },
        )
        self.db.add(run)
        self.db.flush()
        ensure_transition("run", run.status, RunStatus.RUNNING.value)
        run.status = RunStatus.RUNNING.value
        run.started_at = utcnow()
        task.current_run_id = run.id
        self.transition_task(
            task,
            TaskStatus.RUNNING.value,
            event_type="run_started",
            event_payload={"run_id": run.id, "run_index": run_index, "trigger": trigger},
        )
        usage = dict(task.usage or {})
        usage["runs"] = int(usage.get("runs", 0)) + 1
        task.usage = usage
        self.db.flush()
        return task, run

    def finish_run(
        self,
        run: AgentRun,
        *,
        status: str = RunStatus.COMPLETED.value,
        summary: dict[str, Any] | None = None,
        error: str | None = None,
        token_usage: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> None:
        ensure_transition("run", run.status, status)
        run.status = status
        run.output_summary = summary
        run.error_message = error[:8000] if error else None
        run.token_usage = token_usage or {}
        run.model = model
        run.completed_at = utcnow()
        run.lease_expires_at = None
        self.db.flush()

    def renew_run(self, run: AgentRun, *, lease_seconds: int = 180) -> None:
        if run.status != RunStatus.RUNNING.value:
            raise ValueError(f"run_not_active:{run.status}")
        run.lease_expires_at = utcnow() + timedelta(seconds=lease_seconds)
        self.db.flush()

    def create_action(
        self,
        *,
        task: AgentTask,
        run: AgentRun,
        capability_id: str,
        arguments: dict[str, Any],
        descriptor_hash: str,
        risk_level: str,
        requires_approval: bool,
        preconditions: dict[str, Any] | None = None,
        effects: dict[str, Any] | None = None,
        target_node_id: str | None = None,
    ) -> AgentAction:
        sequence = int(self.db.query(func.max(AgentAction.sequence)).filter(
            AgentAction.task_id == task.id,
        ).scalar() or 0) + 1
        action = AgentAction(
            task_id=task.id,
            run_id=run.id,
            sequence=sequence,
            capability_id=capability_id,
            target_node_id=target_node_id,
            arguments=arguments,
            arguments_hash=sha256_json(arguments),
            descriptor_hash=descriptor_hash,
            idempotency_key=f"task:{task.id}:action:{sequence}",
            status=ActionStatus.PLANNED.value,
            risk_level=risk_level,
            requires_approval=requires_approval,
            preconditions=preconditions or {},
            effects=effects or {},
        )
        self.db.add(action)
        usage = dict(task.usage or {})
        usage["actions"] = int(usage.get("actions", 0)) + 1
        task.usage = usage
        self.db.flush()
        self.append_event(
            task.id,
            "action_planned",
            {"capability_id": capability_id, "sequence": sequence, "risk_level": risk_level},
            run_id=run.id,
            action_id=action.id,
        )
        return action

    def transition_action(
        self,
        action: AgentAction,
        status: str,
        *,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        ensure_transition("action", action.status, status)
        action.status = status
        action.updated_at = utcnow()
        if status in {ActionStatus.DISPATCHED.value, ActionStatus.RUNNING.value} and action.started_at is None:
            action.started_at = utcnow()
        if status in {
            ActionStatus.SUCCEEDED.value, ActionStatus.FAILED.value,
            ActionStatus.CANCELLED.value, ActionStatus.INVALIDATED.value,
        }:
            action.completed_at = utcnow()
        if error:
            action.error_message = error[:8000]
        self.db.flush()
        self.append_event(
            action.task_id,
            f"action_{status}",
            payload or {"status": status, "capability_id": action.capability_id},
            run_id=action.execution_run_id or action.run_id,
            action_id=action.id,
        )

    def latest_checkpoint(self, task_id: str) -> AgentTaskCheckpoint | None:
        return self.db.query(AgentTaskCheckpoint).filter(
            AgentTaskCheckpoint.task_id == task_id,
        ).order_by(AgentTaskCheckpoint.version.desc()).first()

    def ready_action(self, task_id: str) -> AgentAction | None:
        return self.db.query(AgentAction).filter(
            AgentAction.task_id == task_id,
            AgentAction.status == ActionStatus.READY.value,
        ).order_by(AgentAction.sequence.asc()).first()

    def cancel_task(self, task_id: str, *, reason: str = "user_cancelled") -> AgentTask:
        task = self.get_task(task_id, for_update=True)
        if task is None:
            raise ValueError("task_not_found")
        if task.status in TERMINAL_TASK_STATES:
            return task
        self.transition_task(
            task, TaskStatus.CANCELLED.value,
            event_type="task_cancelled",
            event_payload={"reason": reason},
            visibility="conversation",
        )
        pending_actions = self.db.query(AgentAction).filter(
            AgentAction.task_id == task.id,
            AgentAction.status.in_([
                ActionStatus.PLANNED.value, ActionStatus.VALIDATED.value,
                ActionStatus.AWAITING_APPROVAL.value, ActionStatus.READY.value,
            ]),
        ).order_by(AgentAction.sequence.asc()).all()
        for action in pending_actions:
            self.transition_action(
                action,
                ActionStatus.CANCELLED.value,
                payload={"reason": reason, "capability_id": action.capability_id},
                error=reason,
            )
        self.db.query(ApprovalRequest).filter(
            ApprovalRequest.task_id == task.id,
            ApprovalRequest.status == "pending",
        ).update({
            ApprovalRequest.status: "denied",
            ApprovalRequest.decision: "deny",
            ApprovalRequest.error_message: reason,
            ApprovalRequest.decided_at: utcnow(),
            ApprovalRequest.completed_at: utcnow(),
        }, synchronize_session=False)
        self.checkpoint(task.id, reason="cancelled")
        return task

    def send_input(self, task_id: str, content: str) -> AgentTask:
        task = self.get_task(task_id, for_update=True)
        if task is None:
            raise ValueError("task_not_found")
        if task.status in TERMINAL_TASK_STATES:
            raise ValueError("task_terminal")
        state = dict(task.task_state or {})
        inputs = list(state.get("inputs") or [])
        inputs.append({"content": content, "created_at": utcnow().isoformat()})
        state["inputs"] = inputs[-50:]
        task.task_state = state
        self.append_event(
            task.id, "user_input_received", {"content": content}, visibility="task",
        )
        # 用户文本只能解除明确的 user-input 阻塞。尤其不能把 unknown Action
        # 从 reconciling 提前改回 queued，否则可能与仍在 Node 执行的副作用并发。
        if task.status == TaskStatus.BLOCKED_USER.value:
            self.transition_task(task, TaskStatus.QUEUED.value)
            self.enqueue_run(task, trigger="user_input")
        else:
            task.version += 1
        self.checkpoint(task.id, reason="user_input")
        return task

    def summary(self, task: AgentTask) -> dict[str, Any]:
        """Task 列表的轻量投影；避免对每张卡片查询完整时间线。"""
        return {
            "id": task.id,
            "thread_id": task.thread_id,
            "task_type": task.task_type,
            "title": task.title,
            "goal": task.goal,
            "status": task.status,
            "executor_type": task.executor_type,
            "target_node_id": task.target_node_id,
            "usage": task.usage or {},
            "report": task.report,
            "last_error": task.last_error,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "completed_at": task.completed_at,
            "action_count": int((task.usage or {}).get("actions", 0)),
        }

    def detail(
        self,
        task: AgentTask,
        *,
        include_events: bool = True,
        event_limit: int = 500,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": task.id,
            "thread_id": task.thread_id,
            "task_type": task.task_type,
            "title": task.title,
            "goal": task.goal,
            "status": task.status,
            "executor_type": task.executor_type,
            "target_node_id": task.target_node_id,
            "task_brief": task.task_brief or {},
            "task_state": task.task_state or {},
            "budgets": task.budgets or {},
            "usage": task.usage or {},
            "report": task.report,
            "last_error": task.last_error,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "completed_at": task.completed_at,
        }
        result["runs"] = [
            {
                "id": run.id, "run_index": run.run_index, "trigger": run.trigger,
                "status": run.status, "model": run.model, "token_usage": run.token_usage or {},
                "started_at": run.started_at, "completed_at": run.completed_at,
            }
            for run in self.db.query(AgentRun).filter(AgentRun.task_id == task.id).order_by(AgentRun.run_index.asc()).all()
        ]
        result["actions"] = [
            {
                "id": action.id, "sequence": action.sequence,
                "run_id": action.run_id, "execution_run_id": action.execution_run_id,
                "capability_id": action.capability_id, "status": action.status,
                "risk_level": action.risk_level, "requires_approval": action.requires_approval,
                "arguments": action.arguments, "result_summary": action.result_summary,
                "evidence_refs": action.evidence_refs or [], "error": action.error_message,
                "created_at": action.created_at, "completed_at": action.completed_at,
            }
            for action in self.db.query(AgentAction).filter(AgentAction.task_id == task.id).order_by(AgentAction.sequence.asc()).all()
        ]
        result["evidence"] = [
            {
                "id": item.id, "action_id": item.action_id, "kind": item.kind,
                "summary": item.summary, "mime_type": item.mime_type,
                "content_size": item.content_size, "content_hash": item.content_hash,
                "stored": bool(item.object_key), "created_at": item.created_at,
            }
            for item in self.db.query(TaskEvidence).filter(TaskEvidence.task_id == task.id).order_by(TaskEvidence.created_at.asc()).all()
        ]
        result["artifacts"] = [
            {
                "id": item.id, "action_id": item.action_id, "name": item.name,
                "artifact_type": item.artifact_type, "summary": item.summary,
                "mime_type": item.mime_type, "content_size": item.content_size,
                "content_hash": item.content_hash, "created_at": item.created_at,
            }
            for item in self.db.query(TaskArtifact).filter(TaskArtifact.task_id == task.id).order_by(TaskArtifact.created_at.asc()).all()
        ]
        result["approvals"] = [
            {
                "id": item.id, "action_id": item.action_id, "tool_name": item.tool_name,
                "status": item.status, "risk_snapshot": item.risk_snapshot,
                "preconditions": item.preconditions, "effects": item.effects,
                "expires_at": item.expires_at, "created_at": item.created_at,
            }
            for item in self.db.query(ApprovalRequest).filter(ApprovalRequest.task_id == task.id).order_by(ApprovalRequest.created_at.asc()).all()
        ]
        if include_events:
            event_limit = max(1, min(2000, event_limit))
            events = self.db.query(AgentTaskEvent).filter(
                AgentTaskEvent.task_id == task.id,
            ).order_by(AgentTaskEvent.sequence.desc()).limit(event_limit).all()
            result["events"] = [
                {
                    "id": event.id, "sequence": event.sequence,
                    "event_type": event.event_type, "visibility": event.visibility,
                    "run_id": event.run_id, "action_id": event.action_id,
                    "payload": event.payload or {}, "created_at": event.created_at,
                }
                for event in reversed(events)
            ]
        return result


def recover_stale_tasks(db: Session, *, now: datetime | None = None) -> int:
    """服务重启/租约过期恢复：不重放未知 Action，只重新调度安全状态。"""
    now = now or utcnow()
    recovered = 0
    candidates = db.query(AgentTask).filter(
        AgentTask.status.in_([
            TaskStatus.DISPATCHING.value, TaskStatus.RUNNING.value,
        ])
    ).with_for_update(skip_locked=True).all()
    repo = TaskRepository(db)
    for task in candidates:
        run = db.get(AgentRun, task.current_run_id) if task.current_run_id else None
        lease = run.lease_expires_at if run else None
        if lease is not None and lease.tzinfo is None:
            lease = lease.replace(tzinfo=timezone.utc)
        if run is not None and run.status == RunStatus.RUNNING.value and lease and lease > now:
            continue
        unknown = db.query(AgentAction).filter(
            AgentAction.task_id == task.id,
            AgentAction.status.in_([ActionStatus.DISPATCHED.value, ActionStatus.RUNNING.value, ActionStatus.UNKNOWN.value]),
        ).first()
        if run is not None and run.status == RunStatus.RUNNING.value:
            repo.finish_run(run, status=RunStatus.INTERRUPTED.value, error="worker_lease_expired")
        if unknown is not None:
            if unknown.status != ActionStatus.UNKNOWN.value:
                repo.transition_action(unknown, ActionStatus.UNKNOWN.value, error="worker_lease_expired")
            if task.status != TaskStatus.RECONCILING.value:
                if task.status == TaskStatus.DISPATCHING.value:
                    repo.transition_task(task, TaskStatus.RUNNING.value)
                repo.transition_task(task, TaskStatus.RECONCILING.value)
            repo.append_event(task.id, "task_reconciliation_required", {"action_id": unknown.id})
        else:
            # dispatching/running/reconciling -> queued；reconciling 可直接 queued。
            if task.status == TaskStatus.DISPATCHING.value:
                repo.transition_task(task, TaskStatus.QUEUED.value)
            elif task.status in {TaskStatus.RUNNING.value, TaskStatus.RECONCILING.value}:
                repo.transition_task(task, TaskStatus.QUEUED.value)
            repo.enqueue_run(task, trigger="recovery")
            repo.append_event(task.id, "task_recovered", {"reason": "worker_lease_expired"})
        repo.checkpoint(task.id, reason="recovery")
        recovered += 1
    return recovered


def reconcile_queued_tasks(db: Session, *, limit: int = 100) -> int:
    """为没有活跃 Outbox 唤醒的 queued Task 补发新 Job。"""
    tasks = db.query(AgentTask).filter(
        AgentTask.status == TaskStatus.QUEUED.value,
    ).order_by(AgentTask.priority.desc(), AgentTask.updated_at.asc()).limit(limit).all()
    repo = TaskRepository(db)
    enqueued = 0
    for task in tasks:
        active = db.query(OutboxJob.id).filter(
            OutboxJob.job_type == "agent_task_run",
            OutboxJob.status.in_(["pending", "running"]),
            OutboxJob.operation_id.like(f"agent_task_run:{task.id}:%"),
        ).first()
        if active is not None:
            continue
        repo.enqueue_run(task, trigger=f"reconcile_v{task.version}")
        repo.append_event(task.id, "task_wakeup_reconciled", {"version": task.version})
        enqueued += 1
    return enqueued
