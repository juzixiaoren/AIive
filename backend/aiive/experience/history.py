"""Reviewable experience history, ordered by completion time and stable episode ID."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from aiive.db.models import AgentTask, Event, ExperienceEpisode, ExperienceSignal
from aiive.experience.capture import result_for, signals_by_episode, utc, visible_episodes, visible_signals


class InvalidEpisodeCursor(ValueError):
    pass


def _summary(row: ExperienceEpisode, signals: list[ExperienceSignal]) -> dict[str, Any]:
    return {
        "id": row.id, "kind": row.kind, "task_type": row.task_type, "family": row.family,
        "source_id": row.source_id, "parent_id": row.parent_id, "outcome": result_for(row, signals),
        "uses": row.uses, "metrics": row.metrics, "completed_at": row.completed_at,
        "feedback": next((signal.kind for signal in signals if signal.details.get("method") == "explicit"), None),
    }


def episode_detail(db: Session, row: ExperienceEpisode) -> dict[str, Any]:
    signals = visible_signals(db, row.id)
    result = _summary(row, signals)
    result["refs"] = row.refs
    result["signals"] = [
        {"kind": signal.kind, "source_id": signal.source_id, "created_at": signal.created_at,
         "method": signal.details.get("method")} for signal in signals
    ]
    content = ""
    if row.kind == "turn":
        request = db.get(Event, row.refs["request_event_id"])
        reply = db.get(Event, row.refs["assistant_event_id"])
        if request:
            result["title"] = str(request.payload.get("content") or "")[:300]
        if reply and reply.event_type == "llm_response" and not reply.payload.get("protocol_only"):
            content = str(reply.payload.get("content") or "")
    else:
        task = db.get(AgentTask, row.source_id)
        if task:
            result["title"] = task.title
            content = str((task.report or {}).get("summary") or "")
    result["result_preview"] = content[:2000]
    result["result_truncated"] = len(content) > 2000
    return result


def episode_page(db: Session, limit: int, cursor: str | None = None) -> dict[str, Any]:
    model = ExperienceEpisode
    query = select(model).order_by(model.completed_at.desc(), model.id)
    if cursor is not None:
        try:
            stamp, identity = cursor.split("|", 1)
            boundary = utc(datetime.fromisoformat(stamp))
            identity = str(UUID(identity))
        except (ValueError, OverflowError) as exc:
            raise InvalidEpisodeCursor("invalid_episode_cursor") from exc
        query = query.where(or_(model.completed_at < boundary,
                                (model.completed_at == boundary) & (model.id > identity)))
    rows: list[ExperienceEpisode] = []
    size = max(16, limit + 1)
    with closing(db.scalars(query.execution_options(yield_per=size))) as stream:
        for batch in stream.partitions(size):
            rows.extend(visible_episodes(db, batch))
            if len(rows) > limit:
                break
    page = rows[:limit]
    signals = signals_by_episode(db, [row.id for row in page])
    requests = [row.refs["request_event_id"] for row in page if row.kind == "turn"]
    titles = {identity: title for identity, title in db.query(
        Event.id, func.substr(Event.payload["content"].as_string(), 1, 300),
    ).filter(Event.id.in_(requests))} if requests else {}
    tasks = [row.source_id for row in page if row.kind == "task"]
    task_titles = dict(db.query(AgentTask.id, AgentTask.title).filter(AgentTask.id.in_(tasks)).all()) if tasks else {}
    items = [
        {**_summary(row, signals[row.id]), "title": titles.get(row.refs["request_event_id"])
         if row.kind == "turn" else task_titles.get(row.source_id)}
        for row in page
    ]
    following = f"{utc(page[-1].completed_at).isoformat()}|{page[-1].id}" if len(rows) > limit else None
    return {"items": items, "next_cursor": following}
