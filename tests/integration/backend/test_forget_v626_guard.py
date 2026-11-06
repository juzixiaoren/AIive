"""V62.6：Job 来源可见性守卫、执行授权覆盖、精确关联与 claim fencing。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session, sessionmaker

from aiive.db.forget_models import ForgetShield
from aiive.db.models import (
    AgentTask,
    MemoryConflictCase,
    MemoryRecord,
    OutboxJob,
    ToolOperation,
    TurnRecord,
)
from aiive.worker.job_source_guard import (
    BLOCKED_REASON,
    blocked_source_reason,
    resolve_job_sources,
)
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome
from tests.integration.backend.test_forget_v624_scope import (
    _event,
    _id,
    _now,
    _op,
    _thread,
    _turn,
)
from tests.support import ensure_running_claim, persist_running_job


def _shield(db: Session, *, target_type: str, target_id: str, thread_id: str | None = None):
    db.add(ForgetShield(
        id=_id(), forget_operation_id=_id(), selector_type=target_type,
        target_type=target_type, target_id=target_id,
        thread_id=thread_id, cutoff_created_at=_now(),
        status="active", normalized_shield_key=f"v626:{target_type}:{target_id}",
        created_at=_now(),
    ))
    db.commit()


def _patched(monkeypatch, module, name="SessionLocal"):
    pass


def _assert_blocked(result: object, db: Session) -> None:
    assert result.outcome == HandlerOutcome.COMPLETED
    assert str(result.reason).startswith(BLOCKED_REASON)


def test_goal_event_blocked_before_dispatch_when_shielded(
    db_session: Session, monkeypatch,
) -> None:
    from aiive.worker import handlers_goal_events

    thread_id = _thread(db_session)
    turn = _turn(db_session, thread_id, _now())
    ev = _event(db_session, thread_id, "secret", _now(), turn_id=turn.turn_id)
    _shield(db_session, target_type="event", target_id=ev, thread_id=thread_id)
    job = persist_running_job(
        db_session, job_type="goal_event_dispatch",
        payload={"event_id": ev, "event_type": "user_message",
                 "event_payload": {"text": "secret"}},
    )
    monkeypatch.setattr(handlers_goal_events, "SessionLocal",
                        sessionmaker(bind=db_session.get_bind()))
    seen: list[dict] = []
    monkeypatch.setattr(handlers_goal_events, "event_from_job_payload",
                        lambda payload: seen.append(payload))
    result = handlers_goal_events.handle_goal_event_dispatch(job)
    _assert_blocked(result, db_session)
    assert seen == []


def test_goal_trigger_blocked_before_llm_when_shielded(
    db_session: Session, monkeypatch,
) -> None:
    from aiive.worker import handlers_goal_triggers

    thread_id = _thread(db_session)
    turn = _turn(db_session, thread_id, _now())
    ev = _event(db_session, thread_id, "secret", _now(), turn_id=turn.turn_id)
    task = AgentTask(
        id=_id(), thread_id=thread_id, task_type="research", title="t", goal="g",
        status="queued", executor_type="llm", priority=0,
        task_brief={}, task_state={}, created_at=_now(),
    )
    db_session.add(task)
    db_session.flush()
    _shield(db_session, target_type="event", target_id=ev, thread_id=thread_id)
    job = persist_running_job(
        db_session, job_type="goal_trigger_decision",
        payload={"task_id": task.id, "step_id": "s1", "trigger_key": "k",
                 "trigger_event_id": ev, "reason": "触发",
                 "trigger_event_payload": {"text": "secret"}},
    )
    monkeypatch.setattr(handlers_goal_triggers, "SessionLocal",
                        sessionmaker(bind=db_session.get_bind()))

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise AssertionError("LLM 不得被调用")

    monkeypatch.setattr(handlers_goal_triggers, "GoalDecisionEngine", _Boom)
    result = handlers_goal_triggers.handle_goal_trigger_decision(job)
    _assert_blocked(result, db_session)


def test_conflict_question_blocked_before_llm_when_memory_shielded(
    db_session: Session, monkeypatch,
) -> None:
    from aiive.worker import handlers_conflict

    thread_id = _thread(db_session)
    memory = MemoryRecord(
        id=_id(), memory_type="fact", canonical_key="k:v626",
        scope_type="thread", scope_id=thread_id, content="old",
        lifecycle_state="active", validity_state="valid", confidence=0.9,
        created_at=_now(),
    )
    db_session.add(memory)
    db_session.flush()
    case = MemoryConflictCase(
        id=_id(), status="pending", thread_id=thread_id, scope_type="thread",
        scope_id=thread_id, canonical_key="k:v626", memory_type="fact",
        sensitivity="normal", old_memory_id=memory.id, old_record_version=1,
        old_content="old", new_content="new", proposal_id="p1",
        proposal_payload={"content": "p"}, verdict="conflict",
        verdict_reason="reason", decision_source="user", resolution_reason="res",
        idempotency_key="case-v626", created_at=_now(),
    )
    db_session.add(case)
    db_session.flush()
    _shield(db_session, target_type="memory_record", target_id=memory.id)
    job = persist_running_job(
        db_session, job_type="memory_conflict_question",
        payload={"conflict_case_id": case.id, "thread_id": thread_id},
    )
    monkeypatch.setattr(handlers_conflict, "SessionLocal",
                        sessionmaker(bind=db_session.get_bind()))

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise AssertionError("LLM 不得被调用")

    monkeypatch.setattr(handlers_conflict, "ConflictQuestionGenerator", _Boom)
    result = handlers_conflict.handle_memory_conflict_question(job)
    _assert_blocked(result, db_session)


def test_memory_projection_blocked_when_memory_shielded(db_session: Session) -> None:
    from aiive.worker import outbox_handlers

    thread_id = _thread(db_session)
    memory = MemoryRecord(
        id=_id(), memory_type="fact", canonical_key="k:v626b",
        scope_type="thread", scope_id=thread_id, content="old",
        lifecycle_state="active", validity_state="valid", confidence=0.9,
        created_at=_now(),
    )
    db_session.add(memory)
    db_session.flush()
    _shield(db_session, target_type="memory_record", target_id=memory.id)
    for job_type in ("memory_vector_refresh", "core_memory_refresh",
                     "memory_graph_refresh"):
        job = persist_running_job(
            db_session, job_type=job_type,
            payload={"memory_id": memory.id, "record_version": 1},
        )
        handler = {
            "memory_vector_refresh": outbox_handlers.handle_memory_vector_refresh,
            "core_memory_refresh": outbox_handlers.handle_core_memory_refresh,
            "memory_graph_refresh": outbox_handlers.handle_memory_graph_refresh,
        }[job_type]
        result = handler(job)
        _assert_blocked(result, db_session)


def test_tool_operation_blocked_when_turn_shielded(db_session: Session) -> None:
    from aiive.tools import operation_executor

    thread_id = _thread(db_session)
    turn = _turn(db_session, thread_id, _now())
    job = persist_running_job(
        db_session, job_type="tool_operation",
        payload={"tool_operation_id": "op-v626"},
    )
    op = ToolOperation(
        id="op-v626", idempotency_key="idem-v626", thread_id=thread_id,
        turn_record_id=turn.id, turn_id=turn.turn_id, tool_call_id="call-1",
        capability_id="cap", params={}, params_hash="h", descriptor_hash="d",
        status="queued", effect_mode="db_transactional", outbox_job_id=job.id,
        trace_id="t-v626",
    )
    db_session.add(op)
    db_session.commit()
    _shield(db_session, target_type="turn_record", target_id=turn.id,
            thread_id=thread_id)
    result = operation_executor.handle_tool_operation(job)
    _assert_blocked(result, db_session)


def test_claim_fencing_rejects_stale_variants(db_session: Session) -> None:
    from aiive.worker.outbox_execution_auth import refresh_claimed_job

    job = persist_running_job(
        db_session, job_type="goal_event_dispatch",
        payload={"event_id": _id(), "event_type": "user_message"},
    )
    factory = sessionmaker(bind=db_session.get_bind())
    fresh, reason = refresh_claimed_job(job, factory)
    assert fresh is not None and reason == ""

    stale_token = ClaimedJob(
        id=job.id, job_type=job.job_type, payload=job.payload, trace_id=job.trace_id,
        retry_count=0, max_retries=3, claim_token="other", schema_version=1,
        worker_id=job.worker_id,
    )
    assert refresh_claimed_job(stale_token, factory)[0] is None

    empty_token = ClaimedJob(
        id=job.id, job_type=job.job_type, payload=job.payload, trace_id=job.trace_id,
        retry_count=0, max_retries=3, claim_token="", schema_version=1,
        worker_id=job.worker_id,
    )
    assert refresh_claimed_job(empty_token, factory)[0] is None

    db_session.query(OutboxJob).filter(OutboxJob.id == job.id).update(
        {OutboxJob.status: "pending"},
    )
    db_session.commit()
    assert refresh_claimed_job(job, factory)[0] is None

    db_session.query(OutboxJob).filter(OutboxJob.id == job.id).update({
        OutboxJob.status: "running",
        OutboxJob.lease_expires_at: datetime.now(timezone.utc) - timedelta(seconds=5),
    })
    db_session.commit()
    assert refresh_claimed_job(job, factory)[0] is None


def test_conflict_case_and_tool_operation_precise_discovery(
    db_session: Session,
) -> None:
    from aiive.forget.content_scope import load_scope

    thread_id = _thread(db_session)
    turn = _turn(db_session, thread_id, _now())
    ev = _event(db_session, thread_id, "secret", _now(), turn_id=turn.turn_id)
    memory = MemoryRecord(
        id=_id(), memory_type="fact", canonical_key="k:v626c",
        scope_type="thread", scope_id=thread_id, content="old",
        lifecycle_state="active", validity_state="valid", confidence=0.9,
        created_at=_now(),
    )
    db_session.add(memory)
    db_session.flush()
    case = MemoryConflictCase(
        id=_id(), status="pending", thread_id=thread_id, scope_type="thread",
        scope_id=thread_id, canonical_key="k:v626c", memory_type="fact",
        sensitivity="normal", old_memory_id=memory.id, old_record_version=1,
        old_content="old", new_content="new", proposal_id="p2",
        proposal_payload={"content": "p"}, verdict="conflict",
        verdict_reason="reason", decision_source="user", resolution_reason="res",
        idempotency_key="case-v626c", created_at=_now(),
    )
    tool_op = ToolOperation(
        id=_id(), idempotency_key=f"idem-{_id()}", thread_id=thread_id,
        turn_record_id=turn.id, turn_id=turn.turn_id, tool_call_id="call-2",
        capability_id="cap", params={}, params_hash="h", descriptor_hash="d",
        status="committed", effect_mode="db_transactional", outbox_job_id=_id(),
        trace_id="t-v626c",
    )
    db_session.add_all([case, tool_op])
    db_session.flush()
    question_job = OutboxJob(
        id=_id(), operation_id=f"question:{_id()}",
        job_type="memory_conflict_question", status="pending",
        payload={"conflict_case_id": case.id, "question": "q"},
        trace_id=case.id, created_at=_now(),
    )
    tool_job = OutboxJob(
        id=_id(), operation_id=f"tool:{_id()}",
        job_type="tool_operation", status="pending",
        payload={"tool_operation_id": tool_op.id}, created_at=_now(),
    )
    db_session.add_all([question_job, tool_job])
    db_session.commit()
    op_id = _op(db_session, mode="everywhere", selector="thread",
                payload={"thread_id": thread_id})
    scope = load_scope(db_session, op_id, "everywhere")
    assert scope is not None
    assert case.id in scope.conflict_case_ids
    assert tool_op.id in scope.tool_operation_ids
    assert question_job.id in scope.outbox_job_ids
    assert tool_job.id in scope.outbox_job_ids
    assert ev is not None


def test_unparseable_reference_fails_closed() -> None:
    from aiive.worker.outbox_payload_contract import job_reference_ids

    refs = job_reference_ids(
        "memory_extraction",
        {"source_event_ids": [{"nested": "x"}], "user_message": "raw"},
    )
    assert refs.unparseable is True
    refs_ok = job_reference_ids("memory_extraction", {"source_event_ids": ["e1"]})
    assert refs_ok.unparseable is False
    assert refs_ok.event_ids == {"e1"}


def test_new_job_with_new_source_not_blocked(db_session: Session) -> None:
    thread_id = _thread(db_session)
    old_turn = _turn(db_session, thread_id, _now() - timedelta(minutes=5))
    old_ev = _event(db_session, thread_id, "old", _now() - timedelta(minutes=5),
                    turn_id=old_turn.turn_id)
    _shield(db_session, target_type="event", target_id=old_ev, thread_id=thread_id)
    new_ev = _event(db_session, thread_id, "new", _now(), turn_id=old_turn.turn_id)
    job = persist_running_job(
        db_session, job_type="goal_event_dispatch",
        payload={"event_id": new_ev, "event_type": "user_message"},
    )
    db = sessionmaker(bind=db_session.get_bind())()
    try:
        sources = resolve_job_sources(db, job)
        reason = blocked_source_reason(db, job, sources)
    finally:
        db.close()
    assert reason == "", reason


def test_segment_sealing_unresolved_source_fails_closed(db_session: Session) -> None:
    job = persist_running_job(
        db_session, job_type="segment_sealing",
        payload={"segment_id": "seg-x", "compaction_input_id": "missing"},
    )
    db = sessionmaker(bind=db_session.get_bind())()
    try:
        sources = resolve_job_sources(db, job)
        reason = blocked_source_reason(db, job, sources)
    finally:
        db.close()
    assert reason == "compaction_input_missing"


class _FakeConnection:
    """最小连接替身：验证 unlock 失败时物理连接被 invalidate。"""

    def __init__(self, granted: object, *, raise_on_unlock: bool = False):
        self._granted = granted
        self._raise = raise_on_unlock
        self.invalidated = False
        self.closed = False

    def execute(self, *args: object, **kwargs: object):  # noqa: ANN002, ANN003
        if self._raise:
            raise RuntimeError("unlock boom")
        outer = self

        class _Result:
            def scalar(self):
                return outer._granted

        return _Result()

    def commit(self) -> None:
        return None

    def invalidate(self) -> None:
        self.invalidated = True

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "granted,raise_on_unlock",
    [(False, False), (True, True)],
)
def test_unlock_failure_invalidates_connection(granted, raise_on_unlock) -> None:
    """释放失败 / 返回 false 时必须 invalidate，禁止带锁归还连接池。"""
    from aiive.worker.outbox_execution_auth import (
        _AuthorizationState,
        _release_shared,
    )

    conn = _FakeConnection(granted, raise_on_unlock=raise_on_unlock)
    state = _AuthorizationState(connection=conn)
    _release_shared(state)
    assert state.connection is None
    assert conn.invalidated is True
    assert conn.closed is True
