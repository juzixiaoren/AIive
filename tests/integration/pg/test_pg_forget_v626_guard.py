"""PostgreSQL 专项：V62.6 Job 来源守卫、执行授权交接与嵌套 Forget 不死锁。"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from aiive.db.forget_models import ForgetShield
from aiive.db.models import Event, OutboxJob, Thread, TurnRecord
from aiive.forget.phase_a_shield import execute_phase_a_shield
from aiive.worker import handlers_goal_events, outbox_execution_auth
from aiive.worker.job_source_guard import BLOCKED_REASON
from aiive.worker.outbox_dto import HandlerOutcome

pytestmark = pytest.mark.pg


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _thread(s: Session) -> str:
    thread = Thread(id=_id(), title="t", created_at=_now())
    s.add(thread)
    s.flush()
    return thread.id


def _turn(s: Session, thread_id: str) -> TurnRecord:
    turn = TurnRecord(
        id=_id(), thread_id=thread_id, turn_id=_id(), status="completed",
        created_at=_now(),
    )
    s.add(turn)
    s.flush()
    return turn


def _event(s: Session, thread_id: str, text: str, turn_id: str) -> Event:
    ev = Event(
        id=_id(), thread_id=thread_id, turn_id=turn_id, event_type="user_message",
        payload={"text": text}, trace_id=_id(), created_at=_now(),
    )
    s.add(ev)
    s.flush()
    return ev


def _running_job(s: Session, *, job_type: str, payload: dict, worker: str = "w1"):
    from aiive.worker.outbox_dto import ClaimedJob

    token = _id()
    job = OutboxJob(
        id=_id(), operation_id=f"v626:{_id()}", job_type=job_type, status="running",
        payload=dict(payload), claim_token=token, locked_by=worker,
        lease_expires_at=_now() + timedelta(minutes=5), max_retries=3,
        created_at=_now(),
    )
    s.add(job)
    s.commit()
    return ClaimedJob(
        id=job.id, job_type=job_type, payload=dict(payload), trace_id=job.trace_id,
        retry_count=0, max_retries=3, claim_token=token, schema_version=1,
        worker_id=worker,
    )


def _advisory_locks(pg_engine) -> list[tuple[str, bool]]:
    with pg_engine.connect() as conn:
        rows = conn.execute(sa_text(
            "SELECT mode, granted FROM pg_locks WHERE locktype = 'advisory'"
        )).all()
    return [(str(r[0]), bool(r[1])) for r in rows]


def test_pg_goal_event_blocked_after_phase_a_before_purge(
    pg_sessionmaker, monkeypatch,
) -> None:
    """Phase A 提交后、Purge 之前：旧 Goal Event Job 不得继续投递。"""
    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        turn = _turn(s, thread_id)
        ev = _event(s, thread_id, "secret", turn.turn_id)
        # 真实 Phase A：Shield 立即生效，Purge 尚未发生
        execute_phase_a_shield(
            s, mode="everywhere", event_ids=[ev.id], requested_by="v626-test",
        )
        s.commit()
        job = _running_job(
            s, job_type="goal_event_dispatch",
            payload={"event_id": ev.id, "event_type": "user_message",
                     "event_payload": {"text": "secret"}},
        )
        assert s.query(ForgetShield).filter(
            ForgetShield.target_id == ev.id, ForgetShield.status == "active",
        ).count() == 1
    finally:
        s.close()

    seen: list[dict] = []
    monkeypatch.setattr(handlers_goal_events, "SessionLocal", pg_sessionmaker)
    monkeypatch.setattr(handlers_goal_events, "event_from_job_payload",
                        lambda payload: seen.append(payload))
    result = handlers_goal_events.handle_goal_event_dispatch(job)
    assert result.outcome == HandlerOutcome.COMPLETED
    assert str(result.reason).startswith(BLOCKED_REASON)
    assert seen == []


def test_pg_nested_forget_handoff_does_not_deadlock(pg_sessionmaker, pg_engine) -> None:
    """持共享授权时执行 Phase A：必须先释放共享锁再取排他锁，提交后重新获取。"""
    from aiive.worker.outbox_execution_auth import (
        has_execution_authorization,
        release_execution_authorization,
        resume_execution_authorization,
        shared_execution_authorization,
    )

    s0: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s0)
        _turn(s0, thread_id)
        s0.commit()
    finally:
        s0.close()

    outcome: dict = {}
    original_engine = outbox_execution_auth.get_engine
    outbox_execution_auth.get_engine = lambda: pg_engine
    try:
        def worker() -> None:
            with shared_execution_authorization():
                assert has_execution_authorization()
                db: Session = pg_sessionmaker()
                try:
                    release_execution_authorization()
                    execute_phase_a_shield(
                        db, mode="everywhere", thread_id=thread_id,
                        requested_by="v626-test",
                    )
                    db.commit()
                finally:
                    db.close()
                outcome["resumed"] = resume_execution_authorization(timeout_ms=3000)
                assert has_execution_authorization()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(15)
        assert not t.is_alive(), "嵌套 Forget 交接出现死锁"
        assert outcome.get("resumed") is True
    finally:
        outbox_execution_auth.get_engine = original_engine

    locks = _advisory_locks(pg_engine)
    assert locks == [] or all(not granted or mode.startswith("Share") for mode, granted in locks)


def test_pg_forget_waits_for_worker_shared_lock(pg_sessionmaker, pg_engine) -> None:
    """Worker 先获得共享授权：Forget Phase A 必须等待其完成。"""
    from aiive.worker.outbox_execution_auth import shared_execution_authorization

    s0: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s0)
        s0.commit()
    finally:
        s0.close()

    held = threading.Event()
    release = threading.Event()
    forget_done = threading.Event()
    forget_error: dict = {}
    original_engine = outbox_execution_auth.get_engine
    outbox_execution_auth.get_engine = lambda: pg_engine
    try:
        def worker() -> None:
            with shared_execution_authorization():
                held.set()
                release.wait(20)

        def forgetter() -> None:
            db: Session = pg_sessionmaker()
            try:
                execute_phase_a_shield(
                    db, mode="everywhere", thread_id=thread_id, requested_by="v626",
                )
                db.commit()
            except Exception as exc:  # noqa: BLE001
                forget_error["error"] = str(exc)
                db.rollback()
            finally:
                db.close()
                forget_done.set()

        tw = threading.Thread(target=worker, daemon=True)
        tw.start()
        assert held.wait(10)
        tf = threading.Thread(target=forgetter, daemon=True)
        tf.start()
        time.sleep(0.8)
        assert not forget_done.is_set(), "Forget 未等待在途执行授权"
        locks = _advisory_locks(pg_engine)
        assert any(mode == "ShareLock" and granted for mode, granted in locks)
        release.set()
        tw.join(20)
        tf.join(20)
        assert forget_done.is_set()
        assert "error" not in forget_error, forget_error.get("error")
    finally:
        release.set()
        outbox_execution_auth.get_engine = original_engine