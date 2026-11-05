"""V62.4：Outbox 契约/异步阻断、Turn-Event 边界、Task 父子范围、未知结构发现。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session, sessionmaker

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


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _at(seconds: int) -> datetime:
    return _now() + timedelta(seconds=seconds)


def _thread(db: Session) -> str:
    tid = _id()
    db.add(Thread(id=tid))
    db.flush()
    return tid


def _event(db: Session, thread_id: str, text: str, created_at: datetime, turn_id: str | None = None) -> str:
    eid = _id()
    db.add(Event(
        id=eid, trace_id=_id(), thread_id=thread_id, event_type="user_message",
        payload={"text": text}, turn_id=turn_id, created_at=created_at,
    ))
    db.flush()
    return eid


def _turn(db: Session, thread_id: str, created_at: datetime) -> TurnRecord:
    rec = TurnRecord(
        id=_id(), thread_id=thread_id, turn_id=_id(), turn_sequence=1,
        status="completed", request_fingerprint="f", source="chat",
        created_at=created_at,
    )
    db.add(rec)
    db.flush()
    return rec


def _op(db: Session, *, mode: str, selector: str, payload: dict) -> str:
    db.flush()
    op_id = _id()
    db.add(ForgetOperation(
        id=op_id, operation_key=f"forget:{op_id}", mode=mode,
        selector_type=selector, selector_hash="h", status="shielded",
        requested_by="test", target_count=0, shielded_at=_now(),
    ))
    db.add(ForgetSelectorManifest(
        id=_id(), forget_operation_id=op_id, selector_type=selector,
        selector_payload={"selector_type": selector, **payload},
        selector_payload_hash="h", cutoff_created_at=_now(),
    ))
    db.flush()
    return op_id


def test_outbox_payload_contract_scrubs_all_job_types(db_session: Session) -> None:
    thread_id = _thread(db_session)
    ev = _event(db_session, thread_id, "secret", _now())
    task = AgentTask(
        id=_id(), thread_id=thread_id, task_type="research", title="t",
        goal="g", status="queued", executor_type="llm", priority=0,
        task_brief={}, task_state={}, created_at=_now(),
    )
    db_session.add(task)
    db_session.flush()
    jobs = [
        ("memory_extraction", {
            "user_message": "raw user", "reply": "raw reply",
            "source_event_ids": [ev], "source_turn_record_id": _id(),
            "thread_id": thread_id, "operation_id": "m1",
        }),
        ("goal_event_dispatch", {
            "event_id": ev, "event_type": "user_message",
            "event_payload": {"text": "raw event"}, "thread_id": thread_id,
        }),
        ("goal_trigger_decision", {
            "task_id": task.id, "step_id": "s1", "trigger_key": "k",
            "trigger_event_id": ev, "trigger_event_payload": {"text": "raw trigger"},
            "reason": "raw reason", "trace_id": "different-trace",
        }),
        ("reminder_delivery", {
            "task_id": task.id, "event_id": ev, "title": "reminder title",
            "content": "reminder content", "scheduled_at": _now().isoformat(),
        }),
        ("agent_task_run", {
            "task_id": task.id, "trigger": "goal", "goal": "raw goal",
            "reason": "raw reason", "failure_reason": "raw failure",
        }),
        ("memory_conflict_question", {
            "conflict_case_id": _id(), "event_id": ev, "title": "question title",
            "content": "question content", "message": "question message",
            "question": "raw question",
        }),
    ]
    for i, (job_type, payload) in enumerate(jobs):
        db_session.add(OutboxJob(
            id=_id(), operation_id=f"contract:{i}", job_type=job_type,
            status="pending", payload=payload, trace_id="unrelated-trace",
            error_message="raw error", created_at=_now(),
        ))
    unrelated = OutboxJob(
        id=_id(), operation_id="contract:unrelated", job_type="memory_extraction",
        status="pending",
        payload={"user_message": "keep", "reply": "keep", "event_id": _id()},
        trace_id="unrelated", created_at=_now(),
    )
    db_session.add(unrelated)
    op_id = _op(db_session, mode="everywhere", selector="thread",
                payload={"thread_id": thread_id})
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()
    scope = load_scope(db_session, op_id, "everywhere")
    assert scope is not None and verify_scope(db_session, scope).ok

    for job in db_session.query(OutboxJob).filter(
        OutboxJob.id != unrelated.id,
    ).all():
        assert is_redacted_payload(job.payload), job.job_type
        assert not payload_has_content(job.job_type, job.payload), job.job_type
        assert job.error_message == ""
    me = db_session.query(OutboxJob).filter_by(job_type="memory_extraction").first()
    assert me.payload["source_event_ids"] == [ev]
    assert me.payload["thread_id"] == thread_id
    assert me.payload["user_message"] == "[redacted]"
    assert me.payload["reply"] == "[redacted]"
    gd = db_session.query(OutboxJob).filter_by(job_type="goal_event_dispatch").first()
    assert gd.payload["event_id"] == ev and gd.payload["event_payload"] == {}
    gt = db_session.query(OutboxJob).filter_by(job_type="goal_trigger_decision").first()
    assert gt.payload["trigger_event_id"] == ev
    assert gt.payload["trigger_event_payload"] == {}
    assert gt.payload["reason"] == "[redacted]"
    assert db_session.query(OutboxJob).filter_by(id=unrelated.id).first().payload["user_message"] == "keep"

    # 人为注入残留 → Verify 必须失败。
    me.payload = dict(me.payload, user_message="leak")
    gd.payload = dict(gd.payload, event_payload={"text": "leak"})
    gt.payload = dict(gt.payload, trigger_event_payload={"text": "leak"})
    db_session.commit()
    assert not verify_scope(db_session, scope).ok


def test_pending_memory_extraction_does_not_call_llm_after_forget(
    db_session: Session, monkeypatch,
) -> None:
    from aiive.worker import outbox_handlers
    from aiive.worker.outbox_dto import ClaimedJob

    thread_id = _thread(db_session)
    turn = _turn(db_session, thread_id, _now())
    ev = _event(db_session, thread_id, "secret", _now(), turn_id=turn.turn_id)
    db_session.add(ForgetShield(
        id=_id(), forget_operation_id=_id(), selector_type="event",
        target_type="event", target_id=ev, cutoff_created_at=_now(),
        status="active", normalized_shield_key=f"shield:{ev}", created_at=_now(),
    ))
    job = OutboxJob(
        id=_id(), operation_id="memory_extraction:pending", job_type="memory_extraction",
        status="pending",
        payload={
            "user_message": "secret", "reply": "reply", "thread_id": thread_id,
            "source_turn_record_id": turn.id, "source_turn_id": turn.turn_id,
            "source_event_ids": [ev], "assistant_event_id": _id(),
            "message_source": "user", "extraction_mode": "async",
        },
        trace_id=_id(), max_retries=3, created_at=_now(),
    )
    db_session.add(job)
    op_id = _op(db_session, mode="everywhere", selector="event_ids",
                payload={"event_ids": [ev]})
    db_session.add(ForgetTarget(
        id=_id(), forget_operation_id=op_id, target_type="event",
        target_id=ev, batch_no=0, frozen_at=_now(),
    ))
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()

    def _boom():
        raise AssertionError("LLM must not be called for forgotten source")

    monkeypatch.setattr(outbox_handlers, "_get_llm_client", _boom)
    monkeypatch.setattr(
        outbox_handlers, "SessionLocal",
        sessionmaker(bind=db_session.get_bind()),
    )
    claimed = ClaimedJob(
        id=job.id, job_type="memory_extraction", payload=job.payload,
        trace_id=job.trace_id, retry_count=0, max_retries=3,
        claim_token="ct", schema_version=1, worker_id="w",
    )
    result = outbox_handlers.handle_memory_extraction(claimed)
    assert result.outcome.value == "completed"
    assert "extraction_blocked_by_forget" in result.reason
    run = db_session.execute(sa_text(
        "SELECT status FROM memory_ingestion_runs WHERE source_turn_record_id = :tid"
    ), {"tid": turn.id}).scalar()
    assert run == "succeeded"


def test_concurrent_forget_during_llm_blocks_write(db_session: Session, monkeypatch) -> None:
    from types import SimpleNamespace

    from aiive.worker import outbox_handlers
    from aiive.worker.outbox_dto import ClaimedJob

    thread_id = _thread(db_session)
    turn = _turn(db_session, thread_id, _now())
    ev = _event(db_session, thread_id, "secret", _now(), turn_id=turn.turn_id)
    job = OutboxJob(
        id=_id(), operation_id="memory_extraction:race", job_type="memory_extraction",
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
    db_session.add(job)
    db_session.commit()

    def _fake_llm():
        return object()

    def _extract(self, **kwargs):  # noqa: ANN001, ANN003
        # 模拟 LLM 调用期间 Forget 提交：Phase A 检查时 Shield 尚不存在。
        db_session.add(ForgetShield(
            id=_id(), forget_operation_id=_id(), selector_type="event",
            target_type="event", target_id=ev, cutoff_created_at=_now(),
            status="active", normalized_shield_key=f"race:{ev}",
            created_at=_now(),
        ))
        db_session.commit()
        return [SimpleNamespace(
            content="in-flight leak", source_turn_id="", source_turn_record_id="",
            source_event_ids=[ev], execution_mode="",
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
    monkeypatch.setattr(
        outbox_handlers, "SessionLocal",
        sessionmaker(bind=db_session.get_bind()),
    )
    claimed = ClaimedJob(
        id=job.id, job_type="memory_extraction", payload=job.payload,
        trace_id=job.trace_id, retry_count=0, max_retries=3,
        claim_token="ct", schema_version=1, worker_id="w",
    )
    result = outbox_handlers.handle_memory_extraction(claimed)
    assert result.outcome.value == "completed"
    assert "extraction_blocked_by_forget" in result.reason
    assert db_session.execute(sa_text(
        "SELECT COUNT(*) FROM memory_records"
    )).scalar() == 0


def test_goal_handlers_skip_redacted_payload(monkeypatch) -> None:
    from aiive.worker.handlers_goal_events import handle_goal_event_dispatch
    from aiive.worker.handlers_goal_triggers import handle_goal_trigger_decision
    from aiive.worker.outbox_dto import ClaimedJob

    def claimed(job_type: str) -> ClaimedJob:
        return ClaimedJob(
            id=_id(), job_type=job_type, payload={"forget_content_redacted": True},
            trace_id="t", retry_count=0, max_retries=3, claim_token="c",
            schema_version=1, worker_id="w",
        )

    assert handle_goal_event_dispatch(claimed("goal_event_dispatch")).outcome.value == "completed"
    assert handle_goal_trigger_decision(claimed("goal_trigger_decision")).outcome.value == "completed"
    normal = ClaimedJob(
        id=_id(), job_type="goal_event_dispatch",
        payload={"event_id": _id(), "event_type": "unknown_type"},
        trace_id="t", retry_count=0, max_retries=3, claim_token="c",
        schema_version=1, worker_id="w",
    )
    assert "forget_content_redacted" not in handle_goal_event_dispatch(normal).reason


def test_cutoff_new_event_same_turn_preserved(db_session: Session) -> None:
    thread_id = _thread(db_session)
    turn = _turn(db_session, thread_id, _at(-120))
    old_ev = _event(db_session, thread_id, "old", _at(-120), turn.turn_id)
    new_ev = _event(db_session, thread_id, "new", _at(120), turn.turn_id)
    op_id = _op(db_session, mode="everywhere", selector="thread",
                payload={"thread_id": thread_id})
    db_session.commit()
    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()
    assert db_session.query(Event).filter_by(id=old_ev).first().payload == {}
    assert db_session.query(Event).filter_by(id=new_ev).first().payload == {"text": "new"}


def test_time_range_turn_children_only_in_window(db_session: Session) -> None:
    thread_id = _thread(db_session)
    turn = _turn(db_session, thread_id, _at(-3600))
    inside = _event(db_session, thread_id, "inside", _at(-60), turn.turn_id)
    outside = _event(db_session, thread_id, "outside", _at(3600), turn.turn_id)
    op_id = _op(
        db_session, mode="everywhere", selector="time_range",
        payload={"time_from": _at(-120).isoformat(), "time_to": _at(0).isoformat()},
    )
    db_session.commit()
    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()
    assert db_session.query(Event).filter_by(id=inside).first().payload == {}
    assert db_session.query(Event).filter_by(id=outside).first().payload == {"text": "outside"}


def test_explicit_turn_selector_and_derived_summary(db_session: Session) -> None:
    thread_id = _thread(db_session)
    turn = _turn(db_session, thread_id, _at(-3600))
    old_ev = _event(db_session, thread_id, "old", _at(-120), turn.turn_id)
    new_ev = _event(db_session, thread_id, "new", _at(120), turn.turn_id)
    epoch_id, seg_id = _id(), _id()
    db_session.add(Segment(
        id=seg_id, epoch_id=epoch_id, thread_id=thread_id, segment_no=1,
        status="open", start_turn_sequence=1, end_turn_sequence=1,
        created_at=_now(),
    ))
    db_session.add(SegmentSummary(
        id=_id(), segment_id=seg_id, goal="old-derived",
        source_event_ids=[old_ev], summary_version=1, token_count=1,
        created_at=_at(120),
    ))
    db_session.add(SegmentSummary(
        id=_id(), segment_id=_id(), goal="new-derived",
        source_event_ids=[new_ev], summary_version=1, token_count=1,
        created_at=_at(120),
    ))
    op_id = _op(db_session, mode="everywhere", selector="turn_ids",
                payload={"turn_ids": [turn.id]})
    db_session.add(ForgetTarget(
        id=_id(), forget_operation_id=op_id, target_type="turn_record",
        target_id=turn.id, batch_no=0, frozen_at=_now(),
    ))
    db_session.commit()
    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()
    assert db_session.query(Event).filter_by(id=old_ev).first().payload == {}
    assert db_session.query(Event).filter_by(id=new_ev).first().payload == {"text": "new"}
    goals = {row.goal for row in db_session.query(SegmentSummary).all()}
    assert "[redacted]" in goals and "new-derived" in goals


def _mk_task_with_children(
    db: Session, thread_id: str, *, task_at: datetime, child_at: datetime,
) -> tuple[str, str, str]:
    task = AgentTask(
        id=_id(), thread_id=thread_id, task_type="research", title="task",
        goal="goal", status="running", executor_type="llm", priority=0,
        task_brief={}, task_state={"evidence_id": "pending"},
        created_at=task_at,
    )
    db.add(task)
    db.flush()
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
    db.add_all([run, checkpoint, evidence, artifact])
    task.task_state = {"evidence_id": evidence.id}
    return task.id, run.id, evidence.id


def test_task_parent_in_range_child_outside_preserved(db_session: Session) -> None:
    thread_id = _thread(db_session)
    task_id, run_id, ev_id = _mk_task_with_children(
        db_session, thread_id, task_at=_at(-60), child_at=_at(3600),
    )
    op_id = _op(
        db_session, mode="everywhere", selector="time_range",
        payload={"time_from": _at(-120).isoformat(), "time_to": _at(0).isoformat()},
    )
    db_session.commit()
    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()
    assert db_session.query(AgentTask).filter_by(id=task_id).first().title == "[redacted]"
    assert db_session.query(AgentRun).filter_by(id=run_id).first().input_snapshot == {"x": 1}
    assert db_session.query(TaskEvidence).filter_by(id=ev_id).first().summary == "evidence"


def test_task_child_in_range_parent_outside_only_child(db_session: Session) -> None:
    thread_id = _thread(db_session)
    task_id, run_id, ev_id = _mk_task_with_children(
        db_session, thread_id, task_at=_at(-3600), child_at=_at(-60),
    )
    op_id = _op(
        db_session, mode="everywhere", selector="time_range",
        payload={"time_from": _at(-120).isoformat(), "time_to": _at(0).isoformat()},
    )
    db_session.commit()
    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()
    task = db_session.query(AgentTask).filter_by(id=task_id).first()
    assert task.title == "task" and task.goal == "goal"
    assert db_session.query(AgentRun).filter_by(id=run_id).first().input_snapshot == {}
    assert db_session.query(TaskEvidence).filter_by(id=ev_id).first().summary == "[redacted]"
    assert task.task_state["evidence_id"] == "[forgotten]"


def test_unknown_json_structure_discovered_by_candidate_scope(db_session: Session) -> None:
    target_thread = _thread(db_session)
    other_thread = _thread(db_session)
    ev = _event(db_session, target_thread, "target", _now())  # 无 turn_id
    other_ev = _event(db_session, other_thread, "other", _now())
    seg = _id()
    db_session.add(Segment(
        id=seg, epoch_id=_id(), thread_id=target_thread, segment_no=1,
        status="open", start_turn_sequence=1, end_turn_sequence=1,
        created_at=_now(),
    ))
    db_session.add(SegmentSummary(
        id=_id(), segment_id=seg, goal="target",
        source_event_ids=[{"id": ev}], summary_version=1, token_count=1,
        created_at=_now(),
    ))
    db_session.add(SegmentSummary(
        id=_id(), segment_id=_id(), goal="unrelated",
        source_event_ids=[{"id": other_ev}], summary_version=1, token_count=1,
        created_at=_now(),
    ))
    op_id = _op(db_session, mode="everywhere", selector="event_ids",
                payload={"event_ids": [ev]})
    db_session.add(ForgetTarget(
        id=_id(), forget_operation_id=op_id, target_type="event",
        target_id=ev, batch_no=0, frozen_at=_now(),
    ))
    db_session.commit()
    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()
    scope = load_scope(db_session, op_id, "everywhere")
    assert scope is not None
    report = verify_scope(db_session, scope)
    assert report.unknown == 1 and not report.ok
