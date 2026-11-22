"""Content source authorization shared by domain readers and background workers.

This module has no Worker DTO dependency and performs no external side effects.
"""

from __future__ import annotations
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from sqlalchemy.orm import Session
from aiive.forget.visibility_service import ForgetVisibilityService


@dataclass
class SourceReferences:
    """Job 的类型化来源引用。"""

    events: dict[str, datetime | None] = field(default_factory=dict)
    turns: dict[str, datetime | None] = field(default_factory=dict)
    memories: dict[str, datetime | None] = field(default_factory=dict)
    summaries: dict[str, datetime | None] = field(default_factory=dict)
    checkpoints: dict[str, datetime | None] = field(default_factory=dict)
    segments: dict[str, datetime | None] = field(default_factory=dict)
    epochs: dict[str, datetime | None] = field(default_factory=dict)
    thread_ids: set[str] = field(default_factory=set)
    thread_times: dict[str, datetime | None] = field(default_factory=dict)
    #: 非空表示来源无法可靠解析 → fail-closed。
    source_times: list[datetime] = field(default_factory=list)
    unresolved: str = ""

    @property
    def empty(self) -> bool:
        return not any(
            (
                self.events,
                self.turns,
                self.memories,
                self.summaries,
                self.checkpoints,
                self.segments,
                self.epochs,
                self.thread_ids,
                self.source_times,
            )
        )


def blocked_reference_ids(
    db: Session,
    target_type: str,
    ids: Mapping[str, datetime | None],
) -> set[str]:
    if not ids:
        return set()
    return ForgetVisibilityService.blocked_target_ids(
        db, target_type, list(ids.items()), require_resolved=True,
    )


DURABLE_SOURCE_TYPES = frozenset({"event", "turn_record", "memory_record"})


def turn_source_events(db: Session, tokens: Collection[str]) -> dict[str, dict[str, datetime]]:
    """Resolve the same physical/logical Turn aliases for every input token."""
    from aiive.db.models import Event, TurnRecord

    if not tokens:
        return {}
    ids = set(tokens)
    direct = db.query(Event.turn_id.label("token"), Event.id.label("event_id"), Event.created_at).filter(
        Event.turn_id.in_(ids),
    )
    physical = db.query(TurnRecord.id.label("token"), Event.id.label("event_id"), Event.created_at).join(
        Event, Event.turn_id == TurnRecord.turn_id,
    ).filter(TurnRecord.id.in_(ids))
    result: dict[str, dict[str, datetime]] = defaultdict(dict)
    for row in direct.union_all(physical):
        result[row.token][row.event_id] = row.created_at
    return result


def blocked_durable_references(
    db: Session, grouped: Mapping[str, Collection[str]],
) -> set[tuple[str, str]]:
    """Return unsafe source keys, including a Turn whose source Event is unsafe."""
    requested_events = set(grouped.get("event", ()))
    turns = set(grouped.get("turn_record", ()))
    try:
        derived_events = turn_source_events(db, turns)
        events = {identity: None for identity in requested_events | {
            identity for rows in derived_events.values() for identity in rows
        }}
        blocked_events = blocked_reference_ids(db, "event", events)
        blocked_turns = blocked_reference_ids(db, "turn_record", dict.fromkeys(turns))
        blocked_turns.update(token for token, rows in derived_events.items() if blocked_events.intersection(rows))
        blocked_memories = blocked_reference_ids(db, "memory_record", dict.fromkeys(grouped.get("memory_record", ())))
        by_type = {"event": blocked_events, "turn_record": blocked_turns, "memory_record": blocked_memories}
        return {
            (kind, identity) for kind, ids in grouped.items() for identity in ids
            if kind not in by_type or identity in by_type[kind]
        }
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Durable source visibility lookup failed")
        return {(kind, identity) for kind, ids in grouped.items() for identity in ids}


def blocked_sources_reason(
    db: Session,
    sources: SourceReferences,
    job_created_at: datetime | None = None,
) -> str:
    """返回屏蔽原因；空串表示来源可见。"""
    if sources.unresolved:
        return sources.unresolved
    if sources.turns:
        events: dict[str, datetime | None] = {
            identity: stamp
            for rows in turn_source_events(db, sources.turns).values()
            for identity, stamp in rows.items()
        }
        if blocked_reference_ids(db, "event", events):
            return "blocked:turn_source_event"
    # 只使用 ForgetVisibilityService 支持元数据重载的目标类型；segment / epoch
    # 的屏蔽通过其 Summary / Checkpoint 与 Thread 选择器传递，不在本层重复判定
    # （未知 created_at 会被 all_user_data 分支 fail-closed 过度拦截）。
    groups = (
        ("event", sources.events),
        ("turn_record", sources.turns),
        ("memory_record", sources.memories),
        ("segment_summary", sources.summaries),
        ("epoch_checkpoint", sources.checkpoints),
    )
    for target_type, ids in groups:
        if blocked_reference_ids(db, target_type, ids):
            return f"blocked:{target_type}"
    for stamp in sources.source_times:
        if ForgetVisibilityService.is_shielded(db, target_time=stamp):
            return "blocked:source_time"
    for thread_id in sorted(sources.thread_ids):
        source_time = sources.thread_times.get(thread_id, job_created_at)
        if source_time is None:
            return "source_time_missing"
        if ForgetVisibilityService.is_shielded(
            db,
            thread_id=thread_id,
            target_time=source_time,
        ):
            return "blocked:thread"
    return ""
