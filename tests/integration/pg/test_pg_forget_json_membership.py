"""PostgreSQL 专项：JSON 数组成员匹配与宽范围清理。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session, sessionmaker

from aiive.db.forget_models import ForgetOperation, ForgetSelectorManifest, ForgetTarget
from aiive.db.models import (
    Epoch,
    EpochCheckpoint,
    Event,
    Segment,
    SegmentSummary,
    Thread,
)
from aiive.forget.content_scope import (
    count_json_array_members,
    select_json_array_members,
)
from aiive.worker.handlers_forget import _scrub_wide

pytestmark = pytest.mark.pg


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_pg_cutoff_freezes_thread_scope(pg_sessionmaker) -> None:
    from datetime import timedelta

    s: Session = pg_sessionmaker()
    try:
        thread_id = _id()
        s.add(Thread(id=thread_id))
        s.flush()
        old_id, new_id = _id(), _id()
        now = _now()
        s.add(Event(
            id=old_id, trace_id=_id(), thread_id=thread_id,
            event_type="user_message", payload={"text": "old"},
            created_at=now - timedelta(seconds=60),
        ))
        s.add(Event(
            id=new_id, trace_id=_id(), thread_id=thread_id,
            event_type="user_message", payload={"text": "new"},
            created_at=now + timedelta(seconds=60),
        ))
        op_id = _id()
        s.add(ForgetOperation(
            id=op_id, operation_key=f"forget:{op_id}", mode="everywhere",
            selector_type="thread", selector_hash="h", status="shielded",
            requested_by="test", target_count=0, shielded_at=now,
        ))
        s.add(ForgetSelectorManifest(
            id=_id(), forget_operation_id=op_id, selector_type="thread",
            selector_payload={"selector_type": "thread", "thread_id": thread_id},
            selector_payload_hash="h", cutoff_created_at=now,
        ))
        s.commit()

        from aiive.worker.handlers_forget import _scrub_wide
        _scrub_wide(s, op_id)
        s.commit()
        s.expire_all()

        assert s.query(Event).filter_by(id=old_id).first().payload == {}
        assert s.query(Event).filter_by(id=new_id).first().payload == {"text": "new"}
    finally:
        s.close()


def test_pg_json_array_membership_segment_summaries(pg_sessionmaker) -> None:
    s: Session = pg_sessionmaker()
    try:
        for sid, values in (
            ("s-null", None),
            ("s-empty", []),
            ("s-dup", ["e1", "e1"]),
            ("s-nested", [{"id": "e2"}]),
            ("s-hit", ["e9", "e1"]),
        ):
            s.add(SegmentSummary(
                id=sid, segment_id=sid, source_event_ids=values,
                summary_version=1, token_count=0, created_at=_now(),
            ))
        s.commit()

        assert set(select_json_array_members(
            s, "segment_summaries", "id", "source_event_ids", ["e1"],
        )) == {"s-dup", "s-hit"}
        assert count_json_array_members(
            s, "segment_summaries", "source_event_ids", ["e1"],
        ) == 2
        assert count_json_array_members(
            s, "segment_summaries", "source_event_ids", ["e2"],
        ) == 0
        assert count_json_array_members(
            s, "segment_summaries", "source_event_ids", [],
        ) == 0
    finally:
        s.close()


def test_pg_json_array_membership_epoch_checkpoints(pg_sessionmaker) -> None:
    s: Session = pg_sessionmaker()
    try:
        s.add(EpochCheckpoint(
            id="c1", epoch_id="ep", current_goal="g",
            source_segment_ids=["seg-1", "seg-2"], source_hashes=["h"],
            version=1, token_count=0, created_at=_now(),
        ))
        s.add(EpochCheckpoint(
            id="c2", epoch_id="ep2", current_goal="g",
            source_segment_ids=None, source_hashes=None,
            version=1, token_count=0, created_at=_now(),
        ))
        s.commit()

        assert select_json_array_members(
            s, "epoch_checkpoints", "id", "source_segment_ids", ["seg-2"],
        ) == ["c1"]
        assert count_json_array_members(
            s, "epoch_checkpoints", "source_segment_ids", ["seg-1", "seg-2"],
        ) == 1
    finally:
        s.close()


def test_pg_wide_event_scrub_uses_json_membership(pg_sessionmaker) -> None:
    s: Session = pg_sessionmaker()
    try:
        thread_id = _id()
        s.add(Thread(id=thread_id))
        s.flush()
        ev = _id()
        s.add(Event(
            id=ev, trace_id=_id(), thread_id=thread_id,
            event_type="user_message", payload={"text": "secret"}, created_at=_now(),
        ))
        seg_id, epoch_id = _id(), _id()
        s.add(Epoch(
            id=epoch_id, thread_id=thread_id, epoch_no=1, status="open",
            created_at=_now(),
        ))
        s.flush()
        s.add(Segment(
            id=seg_id, epoch_id=epoch_id, thread_id=thread_id, segment_no=1,
            status="open", start_turn_sequence=1, end_turn_sequence=1,
            created_at=_now(),
        ))
        s.add(SegmentSummary(
            id=_id(), segment_id=seg_id, goal="goal", outcome="outcome",
            decisions=["d"], source_event_ids=[ev], source_turn_ids=[],
            source_hash="h", summary_version=1, token_count=1, created_at=_now(),
        ))
        s.add(EpochCheckpoint(
            id=_id(), epoch_id=epoch_id, current_goal="goal",
            completed_milestones=["m"], source_segment_ids=[seg_id],
            source_hashes=["h"], version=1, token_count=1, created_at=_now(),
        ))
        op_id = _id()
        s.add(ForgetOperation(
            id=op_id, operation_key=f"forget:{op_id}", mode="everywhere",
            selector_type="event_ids", selector_hash="h", status="shielded",
            requested_by="test", target_count=1, shielded_at=_now(),
        ))
        s.add(ForgetSelectorManifest(
            id=_id(), forget_operation_id=op_id, selector_type="event_ids",
            selector_payload={"selector_type": "event_ids", "event_ids": [ev]},
            selector_payload_hash="h", cutoff_created_at=_now(),
        ))
        s.add(ForgetTarget(
            id=_id(), forget_operation_id=op_id, target_type="event",
            target_id=ev, batch_no=0, frozen_at=_now(),
        ))
        s.commit()

        _scrub_wide(s, op_id)
        s.commit()

        s.expire_all()
        assert s.query(Event).filter_by(id=ev).first().payload == {}
        summary = s.query(SegmentSummary).filter_by(segment_id=seg_id).first()
        assert summary is not None and summary.goal == "[redacted]"
        assert summary.source_event_ids == [ev]
        checkpoint = s.query(EpochCheckpoint).filter_by(epoch_id=epoch_id).first()
        assert checkpoint is not None and checkpoint.current_goal == "[redacted]"
        assert checkpoint.source_segment_ids == [seg_id]
    finally:
        s.close()
