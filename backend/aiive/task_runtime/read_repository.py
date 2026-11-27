"""Safe default Task aggregate reads; raw persistence stays in TaskRepository.

All API/tool consumers use these domain methods, not Forget internals.
"""

from __future__ import annotations
import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime
from itertools import batched
from typing import Any, TypeVar, cast
from sqlalchemy import Select, or_, select, tuple_
from sqlalchemy.orm import InstrumentedAttribute, Session, load_only
from aiive.db.models import (
    AgentTask,
    ApprovalRequest,
    AgentAction,
    AgentRun,
    AgentTaskEvent,
    AgentTaskCheckpoint,
    TaskEvidence,
    TaskArtifact,
    Event,
)
from aiive.db.forget_models import ContentProvenanceRef
from aiive.forget.source_policy import blocked_durable_references
from aiive.forget.visibility_service import ForgetVisibilityService

ChildObject = ApprovalRequest | AgentAction | AgentRun | AgentTaskEvent | AgentTaskCheckpoint | TaskEvidence | TaskArtifact
Child = TypeVar("Child", bound=ChildObject)
logger = logging.getLogger(__name__)

TASK_VISIBILITY_COLUMNS = (
    AgentTask.id, AgentTask.thread_id, AgentTask.source_turn_record_id,
    AgentTask.created_at, AgentTask.task_state, AgentTask.report,
)


class TaskContentUnavailable(PermissionError):
    """A protected object is absent, unresolved, or no longer readable."""


def inherit_sources(
    db: Session, owner_type: str, owner_id: str, parents: Sequence[tuple[str, str]]
) -> None:
    if not parents:
        return
    ref = ContentProvenanceRef
    columns = db.query(ref.source_type, ref.source_id)
    existing = {(kind, identity) for kind, identity in columns.filter_by(
        owner_type=owner_type, owner_id=owner_id,
    )}
    inherited: set[tuple[str, str]] = set()
    # Keep composite IN predicates below SQLite's legacy bind parameter limit.
    for group in batched(dict.fromkeys(parents), 400):
        rows = columns.filter(tuple_(ref.owner_type, ref.owner_id).in_(group)).distinct()
        inherited.update((kind, identity) for kind, identity in rows)
    db.add_all([
        ref(owner_type=owner_type, owner_id=owner_id, source_type=kind, source_id=identity)
        for kind, identity in sorted(inherited - existing)
    ])


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


OwnerKey = tuple[str, str]


class ReadVisibilityContext:
    """One read attempt's source and dependency results."""

    def __init__(self, db: Session) -> None:
        self.db: Session = db
        self.proofs: dict[OwnerKey, tuple[bool, bool]] = {}
        self.sources: dict[OwnerKey, bool] = {}
        self.tasks: dict[str, bool] = {}
        self.plans: dict[str, bool] = {}
        self.children: dict[OwnerKey, bool] = {}

    def _load_proofs(self, owners: set[OwnerKey], turns: set[str]) -> None:
        by_type: dict[str, set[str]] = defaultdict(set)
        for kind, identity in owners - self.proofs.keys():
            by_type[kind].add(identity)
        refs: dict[OwnerKey, set[OwnerKey]] = defaultdict(set)
        new_sources: dict[str, set[str]] = defaultdict(set)

        def include(kind: str, identity: str) -> None:
            if (kind, identity) not in self.sources:
                new_sources[kind].add(identity)

        for identity in turns:
            include("turn_record", identity)
        if by_type:
            try:
                rows = self.db.query(
                    ContentProvenanceRef.owner_type, ContentProvenanceRef.owner_id,
                    ContentProvenanceRef.source_type, ContentProvenanceRef.source_id,
                ).filter(or_(*(
                    (ContentProvenanceRef.owner_type == kind) & ContentProvenanceRef.owner_id.in_(ids)
                    for kind, ids in by_type.items()
                ))).all()
            except Exception:
                # Failed lookup must never activate a legacy "no source" rule.
                logger.exception("Task provenance lookup failed")
                self.proofs.update((key, (True, False)) for key in owners - self.proofs.keys())
                self.sources.update((("turn_record", identity), False) for identity in turns)
                return
            for kind, identity, source_type, source_id in rows:
                refs[kind, identity].add((source_type, source_id))
                include(source_type, source_id)

        blocked = blocked_durable_references(self.db, new_sources)
        for kind, ids in new_sources.items():
            self.sources.update(((kind, identity), (kind, identity) not in blocked) for identity in ids)
        for key in owners - self.proofs.keys():
            self.proofs[key] = (bool(refs[key]), all(self.sources[ref] for ref in refs[key]))

    def _plan_visible(self, task: AgentTask) -> bool:
        if task.id not in self.plans:
            has_sources, visible = self.proofs["goal_plan", task.id]
            if not has_sources:
                goal = (task.task_state or {}).get("goal") or {}
                try:
                    visible = isinstance(goal, dict) and int(goal.get("revision") or 1) <= 1
                except (TypeError, ValueError):
                    visible = False
            self.plans[task.id] = visible
        return self.plans[task.id]

    def plan_visible(self, task: AgentTask) -> bool:
        self._load_proofs({("goal_plan", task.id)}, set())
        return self._plan_visible(task)

    def resolve(
        self, tasks: Sequence[AgentTask], groups: Mapping[str, Sequence[ChildObject]],
    ) -> None:
        children = {
            (kind, row.id): row for kind, rows in groups.items() for row in rows
            if (kind, row.id) not in self.children
        }
        # Expand Event -> Action -> Evidence in topological order; query each layer once.
        dependencies: dict[OwnerKey, OwnerKey | None] = {}
        for key, row in children.items():
            if isinstance(row, AgentTaskEvent) and row.action_id:
                dependencies[key] = ("agent_action", row.action_id)
        self._load_children(children, dependencies, "agent_action", AgentAction, (
            AgentAction.id, AgentAction.task_id, AgentAction.result_summary,
        ))
        for key, row in children.items():
            if isinstance(row, AgentAction) and row.result_summary:
                summary = cast(object, row.result_summary)
                if not isinstance(summary, dict) or "evidence_id" in summary:
                    evidence_id = summary.get("evidence_id") if isinstance(summary, dict) else None
                    dependencies[key] = ("task_evidence", evidence_id) if isinstance(evidence_id, str) else None
        self._load_children(children, dependencies, "task_evidence", TaskEvidence, (
            TaskEvidence.id, TaskEvidence.task_id,
        ))

        pending_tasks = {task.id: task for task in tasks if task.id not in self.tasks}
        missing = {row.task_id for row in children.values() if row.task_id} - self.tasks.keys() - pending_tasks.keys()
        if missing:
            pending_tasks.update(
                (row.id, row) for row in self.db.query(AgentTask)
                .options(load_only(*TASK_VISIBILITY_COLUMNS)).filter(AgentTask.id.in_(missing))
            )
        self._load_proofs(
            set(children) | {(kind, identity) for identity in pending_tasks for kind in ("agent_task", "goal_plan")},
            {task.source_turn_record_id for task in pending_tasks.values() if task.source_turn_record_id},
        )
        blocked_threads = ForgetVisibilityService.blocked_thread_targets(
            self.db, {task.id: (task.thread_id, task.created_at) for task in pending_tasks.values()},
        )
        for task in pending_tasks.values():
            has_sources, visible = self.proofs["agent_task", task.id]
            if not has_sources:
                state = task.task_state or {}
                visible = not (state.get("inputs") or state.get("last_decision") or task.report)
            self.tasks[task.id] = (
                task.id not in blocked_threads
                and (not task.source_turn_record_id or self.sources["turn_record", task.source_turn_record_id])
                and visible and self._plan_visible(task)
            )
        # Evidence (and independent children) before Action before Event.
        for key, row in sorted(children.items(), key=lambda pair: (
            isinstance(pair[1], AgentTaskEvent), isinstance(pair[1], AgentAction),
        )):
            has_sources, visible = self.proofs[key]
            if not has_sources:
                visible = not (
                    isinstance(row, (TaskEvidence, TaskArtifact, AgentTaskCheckpoint))
                    or (isinstance(row, AgentAction) and row.result_summary and key not in dependencies)
                )
            if key in dependencies:
                dependency = dependencies[key]
                visible = visible and dependency is not None and self.children.get(dependency, False)
            self.children[key] = self.tasks.get(row.task_id or "", False) and visible

    def _load_children(
        self, children: dict[OwnerKey, ChildObject], dependencies: dict[OwnerKey, OwnerKey | None],
        kind: str, model: type[AgentAction] | type[TaskEvidence], columns: tuple[InstrumentedAttribute[Any], ...],
    ) -> None:
        ids = {
            target[1] for target in dependencies.values()
            if target and target[0] == kind and target not in children and target not in self.children
        }
        if ids:
            for row in self.db.query(model).options(load_only(*columns)).filter(model.id.in_(ids)):
                children[kind, row.id] = row


class TaskReadRepository:
    def __init__(self, db: Session, visibility: ReadVisibilityContext | None = None) -> None:
        self.db: Session = db
        self.visibility: ReadVisibilityContext | None = visibility

    def has_sources(self, owner_type: str, owner_id: str) -> bool:
        return (
            self.db.query(ContentProvenanceRef.id)
            .filter_by(owner_type=owner_type, owner_id=owner_id)
            .first()
            is not None
        )

    def plan_visible(self, task: AgentTask) -> bool:
        return (self.visibility or ReadVisibilityContext(self.db)).plan_visible(task)

    def task_visible(self, task: AgentTask) -> bool:
        return task.id in self.task_visible_many([task])

    def task_visible_many(self, tasks: Sequence[AgentTask]) -> set[str]:
        context = self.visibility or ReadVisibilityContext(self.db)
        context.resolve(tasks, {})
        return {task.id for task in tasks if context.tasks.get(task.id)}

    def visible_children_many(self, owner_type: str, items: Sequence[ChildObject]) -> set[str]:
        context = self.visibility or ReadVisibilityContext(self.db)
        context.resolve((), {owner_type: items})
        return {item.id for item in items if context.children.get((owner_type, item.id))}

    def get_task(self, task_id: str, *, for_update: bool = False) -> AgentTask | None:
        query = self.db.query(AgentTask).filter(AgentTask.id == task_id)
        if for_update:
            from aiive.forget.concurrency import acquire_forget_shared_lock

            acquire_forget_shared_lock(self.db)
            query = query.with_for_update()
        task = query.first()
        return task if task is not None and self.task_visible(task) else None

    def visible_child(self, owner_type: str, item: ChildObject) -> bool:
        return item.id in self.visible_children_many(owner_type, [item])

    def filter_children(self, owner_type: str, items: Sequence[Child]) -> list[Child]:
        visible = self.visible_children_many(owner_type, items)
        return [item for item in items if item.id in visible]

    def _recent_visible(
        self, owner_type: str, statement: Select[Child], limit: int,
    ) -> list[Child]:
        if limit <= 0:
            return []
        size = min(max(32, limit), 128)
        result: list[Child] = []
        with closing(self.db.scalars(statement.execution_options(yield_per=size))) as rows:
            for batch in rows.partitions(size):
                result.extend(self.filter_children(owner_type, batch))
                if len(result) >= limit:
                    break
        return result[:limit]

    def actions(self, task_id: str, limit: int = 20) -> list[AgentAction]:
        query = select(AgentAction).filter_by(task_id=task_id).order_by(AgentAction.sequence.desc())
        return self._recent_visible("agent_action", query, limit)

    def artifacts(self, task_id: str, limit: int = 20) -> list[TaskArtifact]:
        query = select(TaskArtifact).filter_by(task_id=task_id).order_by(
            TaskArtifact.created_at.desc(), TaskArtifact.id.desc(),
        )
        return self._recent_visible("task_artifact", query, limit)

    def signals(
        self, task_id: str, event_types: list[str], limit: int = 10
    ) -> list[AgentTaskEvent]:
        query = select(AgentTaskEvent).filter(
            AgentTaskEvent.task_id == task_id, AgentTaskEvent.event_type.in_(event_types),
        ).order_by(AgentTaskEvent.sequence.desc())
        return self._recent_visible("agent_task_event", query, limit)

    def recent_evidence(self, task_id: str, limit: int) -> list[TaskEvidence]:
        query = select(TaskEvidence).filter_by(task_id=task_id).order_by(
            TaskEvidence.created_at.desc(), TaskEvidence.id.desc(),
        )
        return self._recent_visible("task_evidence", query, limit)

    def evidence(self, task_id: str) -> list[TaskEvidence]:
        rows = (
            self.db.query(TaskEvidence)
            .filter_by(task_id=task_id)
            .order_by(TaskEvidence.created_at, TaskEvidence.id)
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
        visible = self.task_visible_many(rows)
        return [task for task in rows if task.id in visible][:limit]


    def task_candidates(self, thread_id: str, before: tuple[datetime, str] | None = None, limit: int = 128) -> list[AgentTask]:
        from sqlalchemy import and_, or_
        query = self.db.query(AgentTask).options(load_only(
            *TASK_VISIBILITY_COLUMNS, AgentTask.title, AgentTask.task_type,
            AgentTask.status, AgentTask.updated_at, AgentTask.target_node_id,
        )).filter_by(thread_id=thread_id)
        if before is not None:
            stamp, task_id = before
            query = query.filter(or_(AgentTask.updated_at < stamp,
                and_(AgentTask.updated_at == stamp, AgentTask.id < task_id)))
        return query.order_by(AgentTask.updated_at.desc(), AgentTask.id.desc()).limit(min(limit, 128)).all()
