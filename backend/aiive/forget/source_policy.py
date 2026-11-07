"""Content source authorization shared by domain readers and background workers.

This module has no Worker DTO dependency and performs no external side effects.
"""

from __future__ import annotations
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
    ids: dict[str, datetime | None],
) -> set[str]:
    if not ids:
        return set()
    meta = ForgetVisibilityService.load_target_meta(db, target_type, list(ids))
    missing = set(ids) - {row["id"] for row in meta if row["created_at"] is not None}
    return missing | ForgetVisibilityService.blocked_target_ids(
        db,
        target_type,
        list(ids.items()),
    )


def blocked_sources_reason(
    db: Session,
    sources: SourceReferences,
    job_created_at: datetime | None = None,
) -> str:
    """返回屏蔽原因；空串表示来源可见。"""
    if sources.unresolved:
        return sources.unresolved
    if sources.turns:
        from sqlalchemy import or_
        from aiive.db.models import Event, TurnRecord

        turns = (
            db.query(TurnRecord)
            .filter(
                or_(
                    TurnRecord.id.in_(sources.turns),
                    TurnRecord.turn_id.in_(sources.turns),
                )
            )
            .all()
        )
        tokens = set(sources.turns) | {turn.turn_id for turn in turns}
        events: dict[str, datetime | None] = {
            row.id: row.created_at
            for row in db.query(Event).filter(Event.turn_id.in_(tokens)).all()
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
