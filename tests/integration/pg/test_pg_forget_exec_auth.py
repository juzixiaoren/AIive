"""PostgreSQL 专项：Outbox 精确关联、共享/排他执行授权真实并发时序。"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from aiive.db.models import (
    Event,
    OutboxJob,
    Thread,
    TurnRecord,
)
from aiive.forget.content_scope import load_scope
from aiive.forget.content_scrub import verify_scope
from aiive.forget.phase_a_shield import execute_phase_a_shield
from aiive.worker import outbox_execution_auth, outbox_handlers
from aiive.worker.handlers_forget import _scrub_wide
from aiive.worker.outbox_dto import ClaimedJob
from aiive.worker.outbox_payload_contract import payload_has_content

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
    from aiive.db.forget_models import ForgetOperation, ForgetSelectorManifest
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


def _advisory_locks(pg_engine) -> list[tuple[str, bool]]:
    from aiive.forget.concurrency import FORGET_LOCK_KEY, FORGET_LOCK_NAMESPACE
    with pg_engine.connect() as conn:
        rows = conn.execute(sa_text(
            "SELECT mode, granted FROM pg_locks WHERE locktype='advisory' "
            "AND classid=:ns AND objid=:key AND objsubid=2"
        ), {"ns": FORGET_LOCK_NAMESPACE, "key": FORGET_LOCK_KEY}).all()
    return [(str(r[0]), bool(r[1])) for r in rows]


def _make_extraction_job(s: Session, thread_id: str, turn: TurnRecord, ev: str,
                         *, created_at: datetime, operation_id: str) -> OutboxJob:
    job = OutboxJob(
        id=_id(), operation_id=operation_id, job_type="memory_extraction",
        status="running",
        payload={
            "user_message": "case secret", "reply": "case reply",
            "thread_id": thread_id, "source_event_ids": [ev],
            "source_turn_record_id": turn.id, "source_turn_id": turn.turn_id,
            "assistant_event_id": _id(), "message_source": "user",
            "extraction_mode": "async",
        },
        trace_id="case-trace", max_retries=3, created_at=created_at,
        claim_token="ct", locked_by="w", lease_expires_at=_at(3600),
    )
    s.add(job)
    s.commit()
    return job


def _claimed(job: OutboxJob) -> ClaimedJob:
    return ClaimedJob(
        id=job.id, job_type="memory_extraction", payload=job.payload,
        trace_id=job.trace_id, retry_count=0, max_retries=3,
        claim_token="ct", schema_version=1, worker_id="w",
    )


def _proposal(ev: str) -> Any:
    from aiive.memory.memory_types import EvidenceItem, MemoryProposal, TrustLevel
    return MemoryProposal(
        memory_type="knowledge", canonical_key="k.exec.auth",
        content="worker produced memory", confidence=0.9,
        source_event_ids=[ev],
        evidence=[EvidenceItem(
            source_event_id=ev, source_type="user_assertion",
            trust_level=TrustLevel.TRUSTED.value,
        )],
    )


def test_pg_shared_authorization_conflicts_with_phase_a_exclusive(pg_engine) -> None:
    from aiive.forget.concurrency import FORGET_LOCK_KEY, FORGET_LOCK_NAMESPACE
    a = pg_engine.connect()
    b = pg_engine.connect()
    try:
        a.execute(sa_text("SELECT pg_advisory_lock_shared(:ns, :key)"),
                  {"ns": FORGET_LOCK_NAMESPACE, "key": FORGET_LOCK_KEY})
        a.commit()
        result: list[str] = []

        def exclusive() -> None:
            try:
                b.execute(sa_text("SET lock_timeout = '600ms'"))
                b.execute(sa_text("SELECT pg_advisory_xact_lock(:ns, :key)"),
                          {"ns": FORGET_LOCK_NAMESPACE, "key": FORGET_LOCK_KEY})
                result.append("acquired")
            except Exception as exc:  # noqa: BLE001
                result.append(type(exc).__name__)
            b.rollback()

        t = threading.Thread(target=exclusive, daemon=True)
        t.start()
        t.join(3)
        assert result and result[0] != "acquired", "排他锁未与共享授权互斥"
        a.execute(sa_text("SELECT pg_advisory_unlock_shared(:ns, :key)"),
                  {"ns": FORGET_LOCK_NAMESPACE, "key": FORGET_LOCK_KEY})
        a.commit()
        result2: list[str] = []

        def exclusive2() -> None:
            b.execute(sa_text("SET lock_timeout='2s'"))
            b.execute(sa_text("SELECT pg_advisory_xact_lock(:ns, :key)"),
                      {"ns": FORGET_LOCK_NAMESPACE, "key": FORGET_LOCK_KEY})
            result2.append("acquired")
            b.rollback()

        t2 = threading.Thread(target=exclusive2, daemon=True)
        t2.start()
        t2.join(3)
        assert result2 == ["acquired"]
    finally:
        try:
            a.execute(sa_text("SELECT pg_advisory_unlock_shared(:ns, :key)"),
                      {"ns": FORGET_LOCK_NAMESPACE, "key": FORGET_LOCK_KEY})
            a.commit()
        except Exception:
            a.rollback()
        a.close()
        b.close()


def test_pg_case_a_worker_first_then_forget(
    pg_sessionmaker, pg_engine, monkeypatch,
) -> None:
    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        turn = _turn(s, thread_id, _at(-120))
        ev = _event(s, thread_id, "old", _at(-120), turn.turn_id)
        op_id = _op(s, selector="thread", payload={"thread_id": thread_id})
        job = _make_extraction_job(
            s, thread_id, turn, ev, created_at=_at(-120),
            operation_id="memory_extraction:case-a",
        )
        llm_started = threading.Event()
        llm_release = threading.Event()
        forget_done = threading.Event()
        worker_result: dict = {}
        forget_result: dict = {}

        def _fake_llm():
            return object()

        def _extract(self, **kwargs):  # noqa: ANN001, ANN003
            llm_started.set()
            llm_release.wait(20)
            return [_proposal(ev)]

        monkeypatch.setattr(outbox_handlers, "_get_llm_client", _fake_llm)
        monkeypatch.setattr(outbox_handlers.UnifiedMemoryExtractor, "extract", _extract)
        monkeypatch.setattr(outbox_handlers, "SessionLocal", pg_sessionmaker)
        monkeypatch.setattr(outbox_execution_auth, "get_engine", lambda: pg_engine)

        def worker() -> None:
            worker_result["result"] = outbox_handlers.handle_memory_extraction(
                _claimed(job),
            )

        def forgetter() -> None:
            db: Session = pg_sessionmaker()
            try:
                forget_result["op"] = execute_phase_a_shield(
                    db, mode="everywhere", thread_id=thread_id,
                    requested_by="test",
                )
                db.commit()
            except Exception as exc:  # noqa: BLE001
                forget_result["error"] = f"{type(exc).__name__}:{exc}"
                db.rollback()
            finally:
                db.close()
                forget_done.set()

        tw = threading.Thread(target=worker, daemon=True)
        tw.start()
        assert llm_started.wait(20), "Worker 未进入 LLM 阶段"
        tf = threading.Thread(target=forgetter, daemon=True)
        tf.start()
        time.sleep(0.8)
        assert not forget_done.is_set(), f"Forget 未等待在途 Worker: {forget_result}"
        locks = _advisory_locks(pg_engine)
        assert any(mode == "ShareLock" and granted for mode, granted in locks)
        assert any(mode == "ExclusiveLock" and not granted for mode, granted in locks)

        llm_release.set()
        tw.join(30)
        tf.join(30)
        assert "error" not in forget_result, forget_result.get("error")
        assert worker_result["result"].outcome.value == "completed"
        assert forget_done.is_set()

        # 后续 Purge 能清理该在途执行产生的记忆
        check = pg_sessionmaker()
        try:
            mem = check.execute(sa_text(
                "SELECT id FROM memory_records WHERE canonical_key = :k"
            ), {"k": "k.exec.auth"}).scalar()
            assert mem
            from aiive.db.forget_models import ForgetTarget
            check.add(ForgetTarget(
                id=_id(), forget_operation_id=op_id,
                target_type="memory_record", target_id=str(mem),
                batch_no=0, frozen_at=_now(),
            ))
            check.commit()
            _scrub_wide(check, op_id)
            check.commit()
            check.expire_all()
            content = check.execute(sa_text(
                "SELECT content FROM memory_records WHERE id = :id"
            ), {"id": str(mem)}).scalar()
            assert content == "[forgotten]"
        finally:
            check.close()
    finally:
        llm_release.set()
        s.close()


def test_pg_case_b_forget_first_then_worker(
    pg_sessionmaker, pg_engine, monkeypatch,
) -> None:
    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        turn = _turn(s, thread_id, _at(-120))
        ev = _event(s, thread_id, "old", _at(-120), turn.turn_id)
        job = _make_extraction_job(
            s, thread_id, turn, ev, created_at=_at(-120),
            operation_id="memory_extraction:case-b",
        )
        phase_a_shielded = threading.Event()
        release_forget = threading.Event()
        forget_done = threading.Event()
        llm_called = threading.Event()
        worker_result: dict = {}

        def _boom():
            llm_called.set()
            raise AssertionError("Forget 已提交，不得启动 LLM")

        monkeypatch.setattr(outbox_handlers, "_get_llm_client", _boom)
        monkeypatch.setattr(outbox_handlers, "SessionLocal", pg_sessionmaker)
        monkeypatch.setattr(outbox_execution_auth, "get_engine", lambda: pg_engine)

        def forgetter() -> None:
            db: Session = pg_sessionmaker()
            try:
                execute_phase_a_shield(
                    db, mode="everywhere", thread_id=thread_id,
                    requested_by="test",
                )
                phase_a_shielded.set()
                release_forget.wait(20)
                db.commit()
            except Exception as exc:  # noqa: BLE001
                worker_result["forget_error"] = f"{type(exc).__name__}:{exc}"
                db.rollback()
            finally:
                db.close()
                forget_done.set()

        tf = threading.Thread(target=forgetter, daemon=True)
        tf.start()
        assert phase_a_shielded.wait(20), f"Forget 未创建 Shield: {worker_result.get('forget_error')}"

        def worker() -> None:
            worker_result["result"] = outbox_handlers.handle_memory_extraction(
                _claimed(job),
            )

        tw = threading.Thread(target=worker, daemon=True)
        tw.start()
        time.sleep(0.8)
        assert not llm_called.is_set(), "Forget 持有排他锁期间 Worker 不得调用 LLM"
        locks = _advisory_locks(pg_engine)
        assert any(mode == "ExclusiveLock" and granted for mode, granted in locks)
        assert any(mode == "ShareLock" and not granted for mode, granted in locks)

        release_forget.set()
        tf.join(30)
        tw.join(30)
        assert "forget_error" not in worker_result, worker_result.get("forget_error")
        assert forget_done.is_set()
        assert not llm_called.is_set(), "Forget 提交后 Worker 仍启动了 LLM"
        assert "extraction_blocked_by_forget" in worker_result["result"].reason
        check = pg_sessionmaker()
        try:
            count = check.execute(sa_text(
                "SELECT COUNT(*) FROM memory_records"
            )).scalar()
            assert count == 0
        finally:
            check.close()
    finally:
        release_forget.set()
        s.close()


def test_pg_outbox_precise_matching_and_nested_list(pg_sessionmaker) -> None:
    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        old_turn = _turn(s, thread_id, _at(-120))
        old_ev = _event(s, thread_id, "old", _at(-120), old_turn.turn_id)
        new_turn = _turn(s, thread_id, _at(120))
        new_ev = _event(s, thread_id, "new", _at(120), new_turn.turn_id)
        op_id = _op(s, selector="thread", payload={"thread_id": thread_id})
        jobs = [
            OutboxJob(
                id=_id(), operation_id="new:extract", job_type="memory_extraction",
                status="pending", created_at=_at(120),
                payload={"user_message": "new", "reply": "new",
                         "thread_id": thread_id, "source_event_ids": [new_ev],
                         "source_turn_record_id": new_turn.id,
                         "source_turn_id": new_turn.turn_id},
                trace_id="new-trace",
            ),
            OutboxJob(
                id=_id(), operation_id="new:goal", job_type="goal_event_dispatch",
                status="pending", created_at=_at(120),
                payload={"event_id": new_ev, "event_type": "user_message",
                         "event_payload": {"text": "new"}, "thread_id": thread_id},
                trace_id="new-trace",
            ),
            OutboxJob(
                id=_id(), operation_id="old:derived", job_type="goal_event_dispatch",
                status="pending", created_at=_at(120),
                payload={"event_id": old_ev, "event_type": "user_message",
                         "event_payload": {"text": "old"}, "thread_id": thread_id},
                trace_id="old-trace",
            ),
            OutboxJob(
                id=_id(), operation_id=f"memory_extraction:{old_turn.id}EXTRA:x",
                job_type="memory_extraction", status="pending", created_at=_at(120),
                payload={"event_id": old_ev + "X", "thread_id": thread_id,
                         "user_message": "collision"},
                trace_id="collision-trace",
            ),
            OutboxJob(
                id=_id(), operation_id="nested:1", job_type="memory_extraction",
                status="pending", created_at=_at(-120),
                payload={
                    "event_id": old_ev, "thread_id": thread_id, "status": "pending",
                    "items": [
                        {"content": "secret", "event_id": old_ev},
                        [[{"reply": "secret", "memory_id": "m"}]],
                    ],
                },
                trace_id="nested-trace",
            ),
        ]
        s.add_all(jobs)
        s.commit()
        _scrub_wide(s, op_id)
        s.commit()
        s.expire_all()
        by_id = {j.id: j for j in s.query(OutboxJob).all()}
        assert by_id[jobs[0].id].payload["user_message"] == "new"
        assert by_id[jobs[1].id].payload["event_payload"] == {"text": "new"}
        assert by_id[jobs[2].id].payload["event_payload"] == {}
        assert by_id[jobs[3].id].payload["user_message"] == "collision"
        nested = by_id[jobs[4].id].payload
        assert nested["items"][0]["content"] == "[redacted]"
        assert nested["items"][1][0][0]["reply"] == "[redacted]"
        assert nested["items"][0]["event_id"] == old_ev
        assert nested["items"][1][0][0]["memory_id"] == "m"
        assert not payload_has_content("memory_extraction", nested)
        scope = load_scope(s, op_id, "everywhere")
        assert scope is not None and verify_scope(s, scope).ok
    finally:
        s.close()
