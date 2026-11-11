"""Safe default Task aggregate reads; raw persistence stays in TaskRepository.

All API/tool consumers use these domain methods, not Forget internals.
"""

from __future__ import annotations
from collections.abc import Sequence
from typing import TypeVar
from sqlalchemy.orm import Session
from aiive.db.models import (
    AgentTask,
    AgentAction,
    AgentRun,
    AgentTaskEvent,
    AgentTaskCheckpoint,
    TaskEvidence,
    TaskArtifact,
    Event,
)
from aiive.db.forget_models import ContentProvenanceRef
from aiive.forget.durable_sources import sources_visible
from aiive.forget.source_policy import SourceReferences, blocked_sources_reason

Child = TypeVar(
    "Child",
    AgentAction,
    AgentRun,
    AgentTaskEvent,
    AgentTaskCheckpoint,
    TaskEvidence,
    TaskArtifact,
)


class TaskContentUnavailable(PermissionError):
    """A protected object is absent, unresolved, or no longer readable."""


def inherit_sources(
    db: Session, owner_type: str, owner_id: str, parents: Sequence[tuple[str, str]]
) -> None:
    existing = {
        (r.source_type, r.source_id)
        for r in db.query(ContentProvenanceRef)
        .filter_by(owner_type=owner_type, owner_id=owner_id)
        .all()
    }
    for parent_type, parent_id in parents:
        for ref in (
            db.query(ContentProvenanceRef)
            .filter_by(owner_type=parent_type, owner_id=parent_id)
            .all()
        ):
            pair = (ref.source_type, ref.source_id)
            if pair not in existing:
                db.add(
                    ContentProvenanceRef(
                        owner_type=owner_type,
                        owner_id=owner_id,
                        source_type=ref.source_type,
                        source_id=ref.source_id,
                    )
                )
                existing.add(pair)


def record_input_source(
    db: Session, task: AgentTask, payload: object, event_id: str = ""
) -> str:
    """Persist an actual server-received input; never invent an old source proof."""
    if event_id:
        from aiive.forget.durable_sources import references_visible

        event = db.get(Event, event_id)
        if (
            event is None
            or event.thread_id != task.thread_id
            or not references_visible(db, {"event": [event_id]})
        ):
            raise ValueError("input_source_unavailable")
        return event_id
    event = Event(
        thread_id=task.thread_id,
        trace_id=task.id,
        event_type="task_input",
        payload={"internal": True, "input": payload},
    )
    db.add(event)
    db.flush()
    return event.id


class TaskReadRepository:
    def __init__(self, db: Session) -> None:
        self.db: Session = db

    def has_sources(self, owner_type: str, owner_id: str) -> bool:
        return (
            self.db.query(ContentProvenanceRef.id)
            .filter_by(owner_type=owner_type, owner_id=owner_id)
            .first()
            is not None
        )

    def plan_visible(self, task: AgentTask) -> bool:
        if self.has_sources("goal_plan", task.id):
            return sources_visible(self.db, "goal_plan", task.id)
        goal = (task.task_state or {}).get("goal") or {}
        try:
            return isinstance(goal, dict) and int(goal.get("revision") or 1) <= 1
        except (TypeError, ValueError):
            return False

    def task_visible(self, task: AgentTask) -> bool:
        refs = SourceReferences(
            thread_ids={task.thread_id}, thread_times={task.thread_id: task.created_at}
        )
        if task.source_turn_record_id:
            refs.turns[task.source_turn_record_id] = None
        if blocked_sources_reason(self.db, refs):
            return False
        if self.has_sources("agent_task", task.id):
            if not sources_visible(self.db, "agent_task", task.id):
                return False
        else:
            state = task.task_state or {}
            if state.get("inputs") or state.get("last_decision") or task.report:
                # Legacy merged state cannot be justified by its original Turn.
                return False
        return self.plan_visible(task)

    def get_task(self, task_id: str, *, for_update: bool = False) -> AgentTask | None:
        query = self.db.query(AgentTask).filter(AgentTask.id == task_id)
        if for_update:
            from aiive.forget.concurrency import acquire_forget_shared_lock

            acquire_forget_shared_lock(self.db)
            query = query.with_for_update()
        task = query.first()
        return task if task is not None and self.task_visible(task) else None

    def visible_child(self, owner_type: str, item: Child) -> bool:
        task = self.db.get(AgentTask, item.task_id)
        if task is None or not self.task_visible(task):
            return False
        if self.has_sources(owner_type, item.id):
            return sources_visible(self.db, owner_type, item.id)
        if isinstance(item, (TaskEvidence, TaskArtifact, AgentTaskCheckpoint)):
            return False
        if isinstance(item, AgentAction):
            if item.result_summary:
                evidence_id = item.result_summary.get("evidence_id")
                evidence = (
                    self.db.get(TaskEvidence, evidence_id)
                    if isinstance(evidence_id, str)
                    else None
                )
                return evidence is not None and self.visible_child(
                    "task_evidence", evidence
                )
        if isinstance(item, AgentTaskEvent) and item.action_id:
            action = self.db.get(AgentAction, item.action_id)
            return action is not None and self.visible_child("agent_action", action)
        return True

    def filter_children(self, owner_type: str, items: Sequence[Child]) -> list[Child]:
        return [item for item in items if self.visible_child(owner_type, item)]

    def actions(self, task_id: str, limit: int = 20) -> list[AgentAction]:
        rows = (
            self.db.query(AgentAction)
            .filter_by(task_id=task_id)
            .order_by(AgentAction.sequence.desc())
            .limit(4096)
            .all()
        )
        return self.filter_children("agent_action", rows)[:limit]

    def artifacts(self, task_id: str, limit: int = 20) -> list[TaskArtifact]:
        rows = (
            self.db.query(TaskArtifact)
            .filter_by(task_id=task_id)
            .order_by(TaskArtifact.created_at.desc())
            .limit(4096)
            .all()
        )
        return self.filter_children("task_artifact", rows)[:limit]

    def signals(
        self, task_id: str, event_types: list[str], limit: int = 10
    ) -> list[AgentTaskEvent]:
        rows = (
            self.db.query(AgentTaskEvent)
            .filter(
                AgentTaskEvent.task_id == task_id,
                AgentTaskEvent.event_type.in_(event_types),
            )
            .order_by(AgentTaskEvent.sequence.desc())
            .limit(4096)
            .all()
        )
        return self.filter_children("agent_task_event", rows)[:limit]

    def evidence(self, task_id: str) -> list[TaskEvidence]:
        rows = (
            self.db.query(TaskEvidence)
            .filter_by(task_id=task_id)
            .order_by(TaskEvidence.created_at)
            .all()
        )
        return self.filter_children("task_evidence", rows)

    def get_evidence(self, task_id: str, evidence_id: str) -> TaskEvidence | None:
        item = self.db.get(TaskEvidence, evidence_id)
        return (
            item
            if item is not None
            and item.task_id == task_id
            and self.visible_child("task_evidence", item)
            else None
        )

    def get_artifact(self, task_id: str, artifact_id: str) -> TaskArtifact | None:
        item = self.db.get(TaskArtifact, artifact_id)
        return (
            item
            if item is not None
            and item.task_id == task_id
            and self.visible_child("task_artifact", item)
            else None
        )

    def get_event(self, event_id: str) -> AgentTaskEvent | None:
        item = self.db.get(AgentTaskEvent, event_id)
        return item if item is not None and self.visible_child("agent_task_event", item) else None

    def event_by_dedupe(self, task_id: str, dedupe_key: str) -> AgentTaskEvent | None:
        item = (
            self.db.query(AgentTaskEvent)
            .filter_by(task_id=task_id, dedupe_key=dedupe_key)
            .one_or_none()
        )
        return (
            item
            if item is not None and self.visible_child("agent_task_event", item)
            else None
        )

    def events(
        self, task_id: str, after_sequence: int = 0, limit: int = 200
    ) -> list[AgentTaskEvent]:
        result: list[AgentTaskEvent] = []
        cursor = after_sequence
        scanned = 0
        while len(result) < limit and scanned < 4096:
            batch = (
                self.db.query(AgentTaskEvent)
                .filter(
                    AgentTaskEvent.task_id == task_id, AgentTaskEvent.sequence > cursor
                )
                .order_by(AgentTaskEvent.sequence)
                .limit(128)
                .all()
            )
            if not batch:
                break
            scanned += len(batch)
            cursor = batch[-1].sequence
            result.extend(self.filter_children("agent_task_event", batch))
        return result[:limit]

    def tasks(
        self, thread_id: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[AgentTask]:
        query = self.db.query(AgentTask)
        if thread_id:
            query = query.filter_by(thread_id=thread_id)
        if status:
            query = query.filter_by(status=status)
        rows = (
            query.order_by(AgentTask.updated_at.desc(), AgentTask.id).limit(4096).all()
        )
        return [task for task in rows if self.task_visible(task)][:limit]


    def task_candidates(self, thread_id: str, before=None, limit: int = 128) -> list[AgentTask]:
        from sqlalchemy import and_, or_
        query = self.db.query(AgentTask).filter_by(thread_id=thread_id)
        if before is not None:
            stamp, task_id = before
            query = query.filter(or_(AgentTask.updated_at < stamp,
                and_(AgentTask.updated_at == stamp, AgentTask.id < task_id)))
        return query.order_by(AgentTask.updated_at.desc(), AgentTask.id.desc()).limit(min(limit, 128)).all()
