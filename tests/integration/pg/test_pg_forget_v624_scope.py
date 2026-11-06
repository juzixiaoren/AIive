"""PostgreSQL 专项：Outbox 契约/异步阻断、Turn-Event、Task 父子范围、未知结构。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from aiive.db.forget_models import (
    ForgetOperation,
    ForgetSelectorManifest,
    ForgetShield,
    ForgetTarget,
)
from aiive.db.models import (
    AgentRun,
    AgentTask,
    AgentTaskCheckpoint,
    Event,
    Epoch,
    OutboxJob,
    Segment,
    SegmentSummary,
    TaskArtifact,
    TaskEvidence,
    Thread,
    TurnRecord,
)
from aiive.forget.content_scope import load_scope
from aiive.forget.content_scrub import verify_scope
from aiive.worker.handlers_forget import _scrub_wide
from aiive.worker.outbox_payload_contract import (
    is_redacted_payload,
    payload_has_content,
)

pytestmark = pytest.mark.pg


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _at(seconds: int) -> datetime:
    return _now() + timedelta(seconds=seconds)


def _thread(s: Session) -> str:
    tid = _id()
    s.add(Thread(id=tid))
    s.flush()
    return tid


def _event(s: Session, thread_id: str, text: str, created_at: datetime,
           turn_id: str | None = None) -> str:
    eid = _id()
    s.add(Event(
        id=eid, trace_id=_id(), thread_id=thread_id, event_type="user_message",
        payload={"text": text}, turn_id=turn_id, created_at=created_at,
    ))
    s.flush()
    return eid


def _turn(s: Session, thread_id: str, created_at: datetime) -> TurnRecord:
    rec = TurnRecord(
        id=_id(), thread_id=thread_id, turn_id=_id(), turn_sequence=1,
        status="completed", request_fingerprint="f", source="chat",
        created_at=created_at,
    )
    s.add(rec)
    s.flush()
    return rec


def _op(s: Session, *, selector: str, payload: dict) -> str:
    op_id = _id()
    s.add(ForgetOperation(
        id=op_id, operation_key=f"forget:{op_id}", mode="everywhere",
        selector_type=selector, selector_hash="h", status="shielded",
        requested_by="test", target_count=0, shielded_at=_now(),
    ))
    s.add(ForgetSelectorManifest(
        id=_id(), forget_operation_id=op_id, selector_type=selector,
        selector_payload={"selector_type": selector, **payload},
        selector_payload_hash="h", cutoff_created_at=_now(),
    ))
    s.flush()
    return op_id


def test_pg_outbox_payload_contract(pg_sessionmaker) -> None:
    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        ev = _event(s, thread_id, "secret", _now())
        task = AgentTask(
            id=_id(), thread_id=thread_id, task_type="research", title="t",
            goal="g", status="queued", executor_type="llm", priority=0,
            task_brief={}, task_state={}, created_at=_now(),
        )
        s.add(task)
        s.flush()
        jobs = [
            ("memory_extraction", {
                "user_message": "raw", "reply": "raw reply",
                "source_event_ids": [ev], "thread_id": thread_id,
                "source_turn_record_id": _id(), "operation_id": "m1",
            }),
            ("goal_event_dispatch", {
                "event_id": ev, "event_type": "user_message",
                "event_payload": {"text": "raw"}, "thread_id": thread_id,
            }),
            ("goal_trigger_decision", {
                "task_id": task.id, "step_id": "s", "trigger_key": "k",
                "trigger_event_id": ev,
                "trigger_event_payload": {"text": "raw"}, "reason": "raw",
            }),
            ("reminder_delivery", {
                "task_id": task.id, "event_id": ev, "title": "title",
                "content": "content",
            }),
            ("agent_task_run", {
                "task_id": task.id, "trigger": "goal", "goal": "raw goal",
                "reason": "raw", "failure_reason": "raw",
            }),
        ]
        for i, (job_type, payload) in enumerate(jobs):
            s.add(OutboxJob(
                id=_id(), operation_id=f"pgcontract:{i}", job_type=job_type,
                status="pending", payload=payload, trace_id="other-trace",
                error_message="err", created_at=_now(),
            ))
        unrelated = OutboxJob(
            id=_id(), operation_id="pgcontract:unrelated", job_type="memory_extraction",
            status="pending",
            payload={"user_message": "keep", "reply": "keep", "event_id": _id()},
            trace_id="other-trace", created_at=_now(),
        )
        s.add(unrelated)
        op_id = _op(s, selector="thread", payload={"thread_id": thread_id})
        s.commit()
        _scrub_wide(s, op_id)
        s.commit()
        s.expire_all()
        scope = load_scope(s, op_id, "everywhere")
        assert scope is not None and verify_scope(s, scope).ok
        for job in s.query(OutboxJob).filter(OutboxJob.id != unrelated.id).all():
            assert is_redacted_payload(job.payload), job.job_type
            assert not payload_has_content(job.job_type, job.payload), job.job_type
        assert s.query(OutboxJob).filter_by(id=unrelated.id).first().payload["user_message"] == "keep"

        me = s.query(OutboxJob).filter_by(job_type="memory_extraction").first()
        me.payload = dict(me.payload, user_message="leak")
        gd = s.query(OutboxJob).filter_by(job_type="goal_event_dispatch").first()
        gd.payload = dict(gd.payload, event_payload={"text": "leak"})
        gt = s.query(OutboxJob).filter_by(job_type="goal_trigger_decision").first()
        gt.payload = dict(gt.payload, trigger_event_payload={"text": "leak"})
        s.commit()
        assert not verify_scope(s, scope).ok
    finally:
        s.close()


def test_pg_pending_memory_extraction_no_llm(pg_sessionmaker, monkeypatch) -> None:
    from aiive.worker import outbox_handlers
    from aiive.worker.outbox_dto import ClaimedJob

    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        turn = _turn(s, thread_id, _now())
        ev = _event(s, thread_id, "secret", _now(), turn.turn_id)
        op_id = _op(s, selector="event_ids", payload={"event_ids": [ev]})
        s.add(ForgetTarget(
            id=_id(), forget_operation_id=op_id, target_type="event",
            target_id=ev, batch_no=0, frozen_at=_now(),
        ))
        s.add(ForgetShield(
            id=_id(), forget_operation_id=op_id, selector_type="event",
            target_type="event", target_id=ev, cutoff_created_at=_now(),
            status="active", normalized_shield_key=f"pg:{ev}", created_at=_now(),
        ))
        job = OutboxJob(
            id=_id(), operation_id="pg_extract:pending", job_type="memory_extraction",
            status="pending",
            payload={
                "user_message": "secret", "reply": "reply", "thread_id": thread_id,
                "source_turn_record_id": turn.id, "source_turn_id": turn.turn_id,
                "source_event_ids": [ev], "assistant_event_id": _id(),
                "message_source": "user", "extraction_mode": "async",
            },
            trace_id=_id(), max_retries=3, created_at=_now(),
        )
        s.add(job)
        s.commit()
        _scrub_wide(s, op_id)
        s.commit()
        s.expire_all()

        def _boom():
            raise AssertionError("LLM must not be called")

        monkeypatch.setattr(outbox_handlers, "_get_llm_client", _boom)
        monkeypatch.setattr(outbox_handlers, "SessionLocal", pg_sessionmaker)
        claimed = ClaimedJob(
            id=job.id, job_type="memory_extraction", payload=job.payload,
            trace_id=job.trace_id, retry_count=0, max_retries=3,
            claim_token="ct", schema_version=1, worker_id="w",
        )
        result = outbox_handlers.handle_memory_extraction(claimed)
        assert result.outcome.value == "completed"
        assert result.reason in ("forget_content_redacted",) or (
            "extraction_blocked_by_forget" in result.reason
        )
    finally:
        s.close()


def test_pg_active_shield_blocks_extraction_before_llm(
    pg_sessionmaker, monkeypatch,
) -> None:
    from aiive.worker import outbox_handlers
    from aiive.worker.outbox_dto import ClaimedJob

    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        turn = _turn(s, thread_id, _now())
        ev = _event(s, thread_id, "secret", _now(), turn.turn_id)
        op_id = _op(s, selector="event_ids", payload={"event_ids": [ev]})
        s.add(ForgetShield(
            id=_id(), forget_operation_id=op_id, selector_type="event",
            target_type="event", target_id=ev, cutoff_created_at=_now(),
            status="active", normalized_shield_key=f"pga3:{ev}", created_at=_now(),
        ))
        job = OutboxJob(
            id=_id(), operation_id="pg_extract:a3", job_type="memory_extraction",
            status="pending",
            payload={
                "user_message": "secret", "reply": "reply", "thread_id": thread_id,
                "source_turn_record_id": turn.id, "source_turn_id": turn.turn_id,
                "source_event_ids": [ev], "assistant_event_id": _id(),
                "message_source": "user", "extraction_mode": "async",
            },
            trace_id=_id(), max_retries=3, created_at=_now(),
        )
        s.add(job)
        s.commit()

        def _boom():
            raise AssertionError("LLM must not be called")

        monkeypatch.setattr(outbox_handlers, "_get_llm_client", _boom)
        monkeypatch.setattr(outbox_handlers, "SessionLocal", pg_sessionmaker)
        claimed = ClaimedJob(
            id=job.id, job_type="memory_extraction", payload=job.payload,
            trace_id=job.trace_id, retry_count=0, max_retries=3,
            claim_token="ct", schema_version=1, worker_id="w",
        )
        result = outbox_handlers.handle_memory_extraction(claimed)
        assert result.outcome.value == "completed"
        assert "extraction_blocked_by_forget" in result.reason
    finally:
        s.close()


def test_pg_cutoff_and_time_range_turn_children(pg_sessionmaker) -> None:
    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        turn = _turn(s, thread_id, _at(-3600))
        old_ev = _event(s, thread_id, "old", _at(-120), turn.turn_id)
        new_ev = _event(s, thread_id, "new", _at(120), turn.turn_id)
        op_id = _op(s, selector="thread", payload={"thread_id": thread_id})
        s.commit()
        _scrub_wide(s, op_id)
        s.commit()
        s.expire_all()
        assert s.query(Event).filter_by(id=old_ev).first().payload == {}
        assert s.query(Event).filter_by(id=new_ev).first().payload == {"text": "new"}

        thread2 = _thread(s)
        turn2 = _turn(s, thread2, _at(-3600))
        inside = _event(s, thread2, "inside", _at(-60), turn2.turn_id)
        outside = _event(s, thread2, "outside", _at(3600), turn2.turn_id)
        op2 = _op(s, selector="time_range", payload={
            "time_from": _at(-120).isoformat(), "time_to": _at(0).isoformat(),
        })
        s.commit()
        _scrub_wide(s, op2)
        s.commit()
        s.expire_all()
        assert s.query(Event).filter_by(id=inside).first().payload == {}
        assert s.query(Event).filter_by(id=outside).first().payload == {"text": "outside"}
    finally:
        s.close()


def _mk_task(s: Session, thread_id: str, task_at: datetime, child_at: datetime):
    task = AgentTask(
        id=_id(), thread_id=thread_id, task_type="research", title="task",
        goal="goal", status="running", executor_type="llm", priority=0,
        task_brief={}, task_state={}, created_at=task_at,
    )
    s.add(task)
    s.flush()
    run = AgentRun(
        id=_id(), task_id=task.id, run_index=0, trigger="user",
        status="running", input_snapshot={"x": 1}, created_at=child_at,
    )
    checkpoint = AgentTaskCheckpoint(
        id=_id(), task_id=task.id, version=1, event_sequence=1,
        state={"x": 1}, plan={}, reason="r", snapshot_hash="h",
        created_at=child_at,
    )
    evidence = TaskEvidence(
        id=_id(), task_id=task.id, kind="file", summary="evidence",
        content_hash="h", mime_type="text/plain", content_size=1,
        created_at=child_at,
    )
    artifact = TaskArtifact(
        id=_id(), task_id=task.id, name="artifact", artifact_type="file",
        summary="artifact", object_bucket="", object_key="", content_hash="h",
        mime_type="text/plain", content_size=1, created_at=child_at,
    )
    s.add_all([run, checkpoint, evidence, artifact])
    task.task_state = {"evidence_id": evidence.id}
    return task.id, run.id, evidence.id


def test_pg_task_parent_child_independent_ranges(pg_sessionmaker) -> None:
    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        task1, run1, ev1 = _mk_task(s, thread_id, _at(-60), _at(3600))
        op1 = _op(s, selector="time_range", payload={
            "time_from": _at(-120).isoformat(), "time_to": _at(0).isoformat(),
        })
        s.commit()
        _scrub_wide(s, op1)
        s.commit()
        s.expire_all()
        assert s.query(AgentTask).filter_by(id=task1).first().title == "[redacted]"
        assert s.query(AgentRun).filter_by(id=run1).first().input_snapshot == {"x": 1}
        assert s.query(TaskEvidence).filter_by(id=ev1).first().summary == "evidence"

        thread2 = _thread(s)
        task2, run2, ev2 = _mk_task(s, thread2, _at(-3600), _at(-60))
        op2 = _op(s, selector="time_range", payload={
            "time_from": _at(-120).isoformat(), "time_to": _at(0).isoformat(),
        })
        s.commit()
        _scrub_wide(s, op2)
        s.commit()
        s.expire_all()
        task = s.query(AgentTask).filter_by(id=task2).first()
        assert task.title == "task"
        assert s.query(AgentRun).filter_by(id=run2).first().input_snapshot == {}
        assert s.query(TaskEvidence).filter_by(id=ev2).first().summary == "[redacted]"
        assert task.task_state["evidence_id"] == "[forgotten]"
    finally:
        s.close()


def test_pg_concurrent_forget_during_llm_blocks_write(pg_sessionmaker, monkeypatch) -> None:
    from types import SimpleNamespace

    from aiive.worker import outbox_handlers
    from aiive.worker.outbox_dto import ClaimedJob

    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        turn = _turn(s, thread_id, _now())
        ev = _event(s, thread_id, "secret", _now(), turn.turn_id)
        op_id = _op(s, selector="event_ids", payload={"event_ids": [ev]})
        s.add(ForgetTarget(
            id=_id(), forget_operation_id=op_id, target_type="event",
            target_id=ev, batch_no=0, frozen_at=_now(),
        ))
        job = OutboxJob(
            id=_id(), operation_id="pg_extract:race", job_type="memory_extraction",
            status="running",
            payload={
                "user_message": "secret", "reply": "reply", "thread_id": thread_id,
                "source_turn_record_id": turn.id, "source_turn_id": turn.turn_id,
                "source_event_ids": [ev], "assistant_event_id": _id(),
                "message_source": "user", "extraction_mode": "async",
            },
            trace_id=_id(), max_retries=3, created_at=_now(),
            claim_token="ct", locked_by="w", lease_expires_at=_at(3600),
        )
        s.add(job)
        s.commit()

        def _fake_llm():
            return object()

        def _extract(self, **kwargs):  # noqa: ANN001, ANN003
            db2: Session = pg_sessionmaker()
            try:
                db2.add(ForgetShield(
                    id=_id(), forget_operation_id=op_id, selector_type="event",
                    target_type="event", target_id=ev,
                    cutoff_created_at=_now(), status="active",
                    normalized_shield_key=f"pgrace:{ev}", created_at=_now(),
                ))
                db2.commit()
            finally:
                db2.close()
            return [SimpleNamespace(
                content="in-flight leak", source_turn_id="",
                source_turn_record_id="", source_event_ids=[ev],
                execution_mode="",
            )]

        def _no_write(*args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("in-flight proposals must not be written")

        monkeypatch.setattr(outbox_handlers, "_get_llm_client", _fake_llm)
        monkeypatch.setattr(
            outbox_handlers.UnifiedMemoryExtractor, "extract", _extract,
        )
        monkeypatch.setattr(
            outbox_handlers.MemoryWriteService, "write_batch", _no_write,
        )
        monkeypatch.setattr(outbox_handlers, "SessionLocal", pg_sessionmaker)
        claimed = ClaimedJob(
            id=job.id, job_type="memory_extraction", payload=job.payload,
            trace_id=job.trace_id, retry_count=0, max_retries=3,
            claim_token="ct", schema_version=1, worker_id="w",
        )
        result = outbox_handlers.handle_memory_extraction(claimed)
        assert result.outcome.value == "completed"
        assert "extraction_blocked_by_forget" in result.reason
        s.expire_all()
        from aiive.db.models import MemoryRecord
        assert s.query(MemoryRecord).count() == 0
    finally:
        s.close()


def test_pg_unknown_json_candidate_scope(pg_sessionmaker) -> None:
    s: Session = pg_sessionmaker()
    try:
        target_thread = _thread(s)
        other_thread = _thread(s)
        ev = _event(s, target_thread, "target", _now())
        other_ev = _event(s, other_thread, "other", _now())
        seg, epoch_id = _id(), _id()
        s.add(Epoch(
            id=epoch_id, thread_id=target_thread, epoch_no=1, status="open",
            created_at=_now(),
        ))
        s.flush()
        s.add(Segment(
            id=seg, epoch_id=epoch_id, thread_id=target_thread, segment_no=1,
            status="open", start_turn_sequence=1, end_turn_sequence=1,
            created_at=_now(),
        ))
        s.add(SegmentSummary(
            id=_id(), segment_id=seg, goal="target",
            source_event_ids=[{"id": ev}], summary_version=1, token_count=1,
            created_at=_now(),
        ))
        s.add(SegmentSummary(
            id=_id(), segment_id=_id(), goal="unrelated",
            source_event_ids=[{"id": other_ev}], summary_version=1,
            token_count=1, created_at=_now(),
        ))
        op_id = _op(s, selector="event_ids", payload={"event_ids": [ev]})
        s.add(ForgetTarget(
            id=_id(), forget_operation_id=op_id, target_type="event",
            target_id=ev, batch_no=0, frozen_at=_now(),
        ))
        s.commit()
        _scrub_wide(s, op_id)
        s.commit()
        s.expire_all()
        scope = load_scope(s, op_id, "everywhere")
        assert scope is not None
        report = verify_scope(s, scope)
        assert report.unknown == 1 and not report.ok
    finally:
        s.close()
