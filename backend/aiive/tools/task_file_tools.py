"""Read evidence and artifacts belonging to this Worker task."""
from __future__ import annotations

import codecs
from typing import Any, override

from aiive.context.run_context import RunContext
from aiive.db.models import AgentTask
from aiive.task_runtime.evidence import ArtifactStore, EvidenceStore, is_text_task_file
from aiive.task_runtime.read_repository import TaskReadRepository, inherit_sources
from aiive.task_runtime.schemas import WorkContinuation
from aiive.tools.base import DatabaseTool
from sqlalchemy.orm import Session


def _task(reader: TaskReadRepository, ctx: RunContext | None) -> AgentTask:
    task = reader.get_task(ctx.task_id) if ctx and ctx.task_id else None
    if task is None or ctx is None or task.thread_id != ctx.thread_id:
        raise ValueError("task_files_unavailable")
    return task


def _file_owner(reader: TaskReadRepository, task: AgentTask, identity: str, kind: str) -> str:
    link = WorkContinuation.from_state(task.task_state)
    if link is not None and identity in (link.evidence_ids if kind == "evidence" else link.artifact_ids):
        from aiive.task_runtime.continuation import visible_continuation_parent

        parent = visible_continuation_parent(reader, task, link)
        if parent is not None:
            return parent.id
    return task.id


class ListTaskFilesTool(DatabaseTool):
    @override
    def _run(self, db: Session, ctx: RunContext | None, kind: str, cursor: str = "", limit: int = 10):
        from aiive.task_runtime.work_read_model import WorkReadModel

        page = WorkReadModel(db).metadata_page(_task(TaskReadRepository(db), ctx).id, kind, cursor or None, limit)
        owner = "task_evidence" if kind == "evidence" else "task_artifact"
        if ctx and ctx.action_id:
            inherit_sources(db, "agent_action", ctx.action_id, [(owner, item["id"]) for item in page["items"]])
        for item in page["items"]:
            item["summary"] = item["summary"][:200]
            item["created_at"] = item["created_at"].isoformat()
        return page


def read_text_task_file(item: Any, read: Any, db: Session, offset: int, max_bytes: int) -> dict[str, Any]:
    if item is None:
        return {"ok": False, "error_type": "task_file_unavailable"}
    mime = item.mime_type
    if not is_text_task_file(mime):
        return {"ok": True, "id": item.id, "mime_type": mime, "size": item.content_size,
                "download_in_work_center": True}
    data, has_more = read(db, item, offset, max_bytes)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    content = decoder.decode(data, final=not has_more)
    consumed = len(data) - len(decoder.getstate()[0])
    return {"ok": True, "id": item.id, "mime_type": mime, "content": content, "offset": offset,
            "next_offset": offset + consumed if has_more else None, "has_more": has_more}


class ReadTaskEvidenceTool(DatabaseTool):
    @override
    def _run(self, db: Session, ctx: RunContext | None, evidence_id: str, offset: int = 0, max_bytes: int = 4096):
        reader = TaskReadRepository(db)
        task = _task(reader, ctx)
        evidence = reader.get_evidence(_file_owner(reader, task, evidence_id, "evidence"), evidence_id)
        if evidence and ctx and ctx.action_id:
            inherit_sources(db, "agent_action", ctx.action_id, [("task_evidence", evidence.id)])
        return read_text_task_file(evidence, EvidenceStore.read, db, offset, max_bytes)


class ReadTaskArtifactTool(DatabaseTool):
    @override
    def _run(self, db: Session, ctx: RunContext | None, artifact_id: str, offset: int = 0, max_bytes: int = 4096):
        reader = TaskReadRepository(db)
        task = _task(reader, ctx)
        artifact = reader.get_artifact(_file_owner(reader, task, artifact_id, "artifact"), artifact_id)
        if artifact and ctx and ctx.action_id:
            inherit_sources(db, "agent_action", ctx.action_id, [("task_artifact", artifact.id)])
        return read_text_task_file(artifact, ArtifactStore.read, db, offset, max_bytes)
