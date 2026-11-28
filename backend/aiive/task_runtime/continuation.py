"""Create and dereference a new report task from selected, visible work."""
from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.orm import Session

from aiive.db.models import AgentTask, TaskArtifact, TaskEvidence
from aiive.forget.concurrency import acquire_forget_shared_lock
from aiive.forget.durable_sources import persist_sources
from aiive.task_runtime.evidence import is_text_task_file
from aiive.task_runtime.read_repository import TaskContentUnavailable, TaskReadRepository
from aiive.task_runtime.repository import TaskRepository
from aiive.task_runtime.schemas import TaskStatus, WorkContinuation


CONTINUABLE_STATUSES = {TaskStatus.SUCCEEDED, TaskStatus.PARTIAL}


def _report(task: AgentTask) -> str:
    summary = (task.report or {}).get("summary")
    return summary.strip() if isinstance(summary, str) else ""


def _selected_files(
    reader: TaskReadRepository, link: WorkContinuation,
) -> tuple[list[TaskArtifact], list[TaskEvidence]]:
    artifacts: list[TaskArtifact] = []
    evidence: list[TaskEvidence] = []
    for identity in link.artifact_ids:
        artifact = reader.get_artifact(link.source_task_id, identity)
        if artifact is None or not is_text_task_file(artifact.mime_type):
            raise TaskContentUnavailable("continuation_source_unavailable")
        artifacts.append(artifact)
    for identity in link.evidence_ids:
        item = reader.get_evidence(link.source_task_id, identity)
        if item is None or not is_text_task_file(item.mime_type):
            raise TaskContentUnavailable("continuation_source_unavailable")
        evidence.append(item)
    return artifacts, evidence


def create_work_continuation(
    db: Session, source_task_id: str, request_id: UUID, instruction: str,
    artifact_ids: list[str], evidence_ids: list[str],
) -> AgentTask:
    acquire_forget_shared_lock(db)
    reader = TaskReadRepository(db)
    # The parent lock serializes retries and concurrent submissions for the same source.
    parent = reader.get_task(source_task_id, for_update=True)
    if parent is None:
        raise TaskContentUnavailable("continuation_source_unavailable")
    if parent.status not in CONTINUABLE_STATUSES:
        raise ValueError("continuation_source_not_finished")
    link = WorkContinuation(
        source_task_id=source_task_id,
        artifact_ids=sorted(set(artifact_ids)), evidence_ids=sorted(set(evidence_ids)),
    )
    if len(link.artifact_ids) + len(link.evidence_ids) > 5:
        raise ValueError("continuation_too_many_files")
    artifacts, evidence = _selected_files(reader, link)
    if not (artifacts or evidence or _report(parent)):
        raise ValueError("continuation_no_source")
    identity = str(uuid5(NAMESPACE_URL, f"aiive:work-continuation:{source_task_id}:{request_id}"))
    repo = TaskRepository(db)
    if (existing := repo.get_raw_task(identity)) is not None:
        if not reader.task_visible(existing):
            raise TaskContentUnavailable("continuation_source_unavailable")
        if existing.goal != instruction or WorkContinuation.from_state(existing.task_state) != link:
            raise ValueError("continuation_request_conflict")
        return existing
    task = repo.create_task(
        task_id=identity, continuation=link, thread_id=parent.thread_id,
        goal=instruction, title=f"续作 · {parent.title}", enqueue=False,
    )
    # Legacy tasks can rely on the Turn Record column without a copied provenance row.
    persist_sources(db, "agent_task", task.id, {"source_turn_record_id": parent.source_turn_record_id})
    repo.enqueue_run(task, trigger="dispatch")
    return task


def visible_continuation_parent(
    reader: TaskReadRepository, task: AgentTask, link: WorkContinuation,
) -> AgentTask | None:
    parent = reader.get_task(link.source_task_id)
    if parent is None or parent.thread_id != task.thread_id or parent.status not in CONTINUABLE_STATUSES:
        return None
    return parent


def continuation_context(reader: TaskReadRepository, task: AgentTask) -> dict[str, Any] | None:
    link = WorkContinuation.from_state(task.task_state)
    if link is None:
        return None
    parent = visible_continuation_parent(reader, task, link)
    if parent is None:
        raise TaskContentUnavailable("continuation_source_unavailable")
    artifacts, evidence = _selected_files(reader, link)
    return {
        "source_task_id": parent.id,
        "original_goal": parent.goal[:1500],
        "report": _report(parent)[:8000],
        "artifacts": [{"id": item.id, "name": item.name, "size": item.content_size} for item in artifacts],
        "evidence": [{"id": item.id, "kind": item.kind, "size": item.content_size} for item in evidence],
    }
