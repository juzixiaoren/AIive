"""Chat attachment binding and explicit durable-task authorization."""

from __future__ import annotations

from typing import Any
from sqlalchemy.orm import Session
from aiive.db.models import AgentTask, Attachment, Event, TurnRecord
from aiive.runtime.attachments import require


def bound_user_attachment(
    db: Session, attachment_id: str, thread_id: str
) -> Attachment:
    row = require(db, attachment_id, thread_id)
    event = db.get(Event, row.event_id) if row.event_id else None
    turn = db.get(TurnRecord, row.turn_record_id) if row.turn_record_id else None
    if (
        event is None
        or turn is None
        or event.event_type != "user_message"
        or event.thread_id != thread_id
        or turn.thread_id != thread_id
        or event.turn_id != turn.turn_id
        or turn.source != "user"
        or (event.payload or {}).get("message_source", "user") != "user"
    ):
        raise ValueError("attachment_not_bound_to_user_message")
    return row


def explicit_ids(value: Any) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(i, str) or not i for i in value
    ):
        raise ValueError("invalid_attachment_ids")
    if len(value) != len(set(value)):
        raise ValueError("duplicate_attachment_ids")
    return list(value)


def validate_task_attachments(
    db: Session, thread_id: str, initial: dict[str, Any]
) -> list[Attachment]:
    ids = explicit_ids(initial.get("attachment_ids", []))
    if ids and initial.get("skill_id") != "document_processing":
        raise ValueError("attachment_skill_required")
    rows = [bound_user_attachment(db, aid, thread_id) for aid in ids]
    from aiive.forget.durable_sources import references_visible

    if not references_visible(
        db, {"event": [row.event_id for row in rows if row.event_id]}
    ):
        raise ValueError("attachment_source_unavailable")
    return rows


def worker_attachment_ids(task: AgentTask) -> list[str]:
    initial = (task.task_brief or {}).get("initial_input") or {}
    if initial.get("skill_id") != "document_processing":
        raise ValueError("attachment_skill_required")
    ids = explicit_ids(initial.get("attachment_ids", []))
    required = explicit_ids((task.task_state or {}).get("required_attachment_ids", []))
    if ids != required:
        raise ValueError("attachment_authorization_unavailable")
    return ids
