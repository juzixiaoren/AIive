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


def _add_shield(s: Session, *, target_type: str, target_id: str, thread_id: str | None = None):
    """走真实 Phase A：同时建立 Shield 与 Tombstone（PG 有 forget_operations 外键）。"""
    kwargs: dict[str, list[str]] = {"event_ids": [], "memory_ids": [], "turn_ids": []}
    key = {
        "event": "event_ids",
        "memory_record": "memory_ids",
        "turn_record": "turn_ids",
    }[target_type]
    kwargs[key] = [target_id]
    execute_phase_a_shield(
        s, mode="everywhere", requested_by="v626-test", **kwargs,
    )
    s.commit()


def _retire(s: Session, target_type: str, target_id: str) -> None:
    from aiive.db.forget_models import ForgetTombstone

    s.query(ForgetShield).filter(
        ForgetShield.target_type == target_type,
        ForgetShield.target_id == target_id,
    ).update({ForgetShield.status: "retired"})
    s.query(ForgetTombstone).filter(
        ForgetTombstone.target_type == target_type,
        ForgetTombstone.target_id == target_id,
    ).update({ForgetTombstone.block_visibility: False})
    s.commit()


def test_pg_segment_sealing_blocked_before_llm(pg_sessionmaker, monkeypatch) -> None:
    """Segment Sealing：来源 Event 被 Shield 后不得调用摘要模型。"""
    from aiive.db.models import CompactionInput, Epoch, Segment
    from aiive.worker import outbox_handlers

    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        turn = _turn(s, thread_id)
        ev = _event(s, thread_id, "secret", turn.turn_id)
        epoch = Epoch(id=_id(), thread_id=thread_id, epoch_no=1, status="active")
        s.add(epoch)
        s.flush()
        segment = Segment(
            id=_id(), epoch_id=epoch.id, thread_id=thread_id, segment_no=1,
            status="sealing", start_turn_sequence=1, end_turn_sequence=1,
            source_hash="h1",
        )
        s.add(segment)
        s.flush()
        ci = CompactionInput(
            id=_id(), segment_id=segment.id, start_turn_sequence=1,
            end_turn_sequence=1, turn_manifest=[{"turn_record_id": turn.id}],
            event_manifest=[{"event_id": ev.id}], working_state_version=1,
            working_state_snapshot={}, source_hash="h1",
        )
        s.add(ci)
        s.commit()
        _add_shield(s, target_type="event", target_id=ev.id, thread_id=thread_id)
        job = _running_job(s, job_type="segment_sealing", payload={
            "segment_id": segment.id, "compaction_input_id": ci.id,
            "source_hash": "h1", "summary_version": 1,
        })
    finally:
        s.close()

    monkeypatch.setattr(outbox_handlers, "SessionLocal", pg_sessionmaker)
    monkeypatch.setattr(
        outbox_handlers, "_get_llm_client",
        lambda: (_ for _ in ()).throw(AssertionError("摘要模型不得被调用")),
    )
    result = outbox_handlers.handle_segment_sealing(job)
    assert result.outcome == HandlerOutcome.COMPLETED
    assert str(result.reason).startswith(BLOCKED_REASON)


def test_pg_epoch_checkpoint_blocked_before_llm(pg_sessionmaker, monkeypatch) -> None:
    """Epoch Checkpoint：Summary 来源 Event 被 Shield 后不得重新生成。"""
    from aiive.db.models import (
        Epoch, EpochCompactionInput, Segment, SegmentSummary,
    )
    from aiive.worker import outbox_handlers

    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        turn = _turn(s, thread_id)
        ev = _event(s, thread_id, "secret", turn.turn_id)
        epoch = Epoch(id=_id(), thread_id=thread_id, epoch_no=1, status="sealing")
        s.add(epoch)
        s.flush()
        segment = Segment(
            id=_id(), epoch_id=epoch.id, thread_id=thread_id, segment_no=1,
            status="sealing", start_turn_sequence=1, end_turn_sequence=1,
            source_hash="h1",
        )
        s.add(segment)
        s.flush()
        summary = SegmentSummary(
            id=_id(), segment_id=segment.id, source_event_ids=[ev.id],
            source_turn_ids=[turn.turn_id], summary_version=1,
        )
        s.add(summary)
        s.flush()
        # sealed 段必须挂 summary（ck_segment_sealed_requires_summary）
        segment.summary_id = summary.id
        segment.status = "sealed"
        eci = EpochCompactionInput(
            id=_id(), epoch_id=epoch.id, boundary_turn_sequence=1,
            working_state_version=1, source_segment_ids=[segment.id],
            source_hashes=["h1"], snapshot_hash="sh1",
        )
        s.add(eci)
        s.commit()
        _add_shield(s, target_type="event", target_id=ev.id, thread_id=thread_id)
        job = _running_job(s, job_type="epoch_checkpoint", payload={
            "epoch_id": epoch.id, "epoch_compaction_input_id": eci.id,
            "boundary_hash": "bh", "checkpoint_version": 1,
        })
    finally:
        s.close()

    monkeypatch.setattr(outbox_handlers, "SessionLocal", pg_sessionmaker)
    monkeypatch.setattr(
        outbox_handlers, "_get_llm_client",
        lambda: (_ for _ in ()).throw(AssertionError("模型不得被调用")),
    )
    result = outbox_handlers.handle_epoch_checkpoint(job)
    assert result.outcome == HandlerOutcome.COMPLETED
    assert str(result.reason).startswith(BLOCKED_REASON)


def test_pg_conflict_question_blocked_before_llm(pg_sessionmaker, monkeypatch) -> None:
    """Conflict Question：案例旧 Memory 被 Shield 后不得生成问题。"""
    from aiive.db.models import MemoryConflictCase, MemoryRecord
    from aiive.worker import handlers_conflict

    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        memory = MemoryRecord(
            id=_id(), memory_type="fact", canonical_key="k:v626pg",
            scope_type="thread", scope_id=thread_id, content="old",
            lifecycle_state="active", validity_state="valid", confidence=0.9,
            created_at=_now(),
        )
        s.add(memory)
        s.flush()
        case = MemoryConflictCase(
            id=_id(), status="pending", thread_id=thread_id, scope_type="thread",
            scope_id=thread_id, canonical_key="k:v626pg", memory_type="fact",
            sensitivity="normal", old_memory_id=memory.id, old_record_version=1,
            old_content="old", new_content="new", proposal_id="p-pg",
            proposal_payload={"content": "p"}, verdict="conflict",
            verdict_reason="reason", decision_source="user",
            resolution_reason="res", idempotency_key="case-v626pg",
            created_at=_now(),
        )
        s.add(case)
        s.commit()
        _add_shield(s, target_type="memory_record", target_id=memory.id)
        job = _running_job(s, job_type="memory_conflict_question", payload={
            "conflict_case_id": case.id, "thread_id": thread_id,
        })
    finally:
        s.close()

    monkeypatch.setattr(handlers_conflict, "SessionLocal", pg_sessionmaker)
    monkeypatch.setattr(
        handlers_conflict, "ConflictQuestionGenerator",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("模型不得被调用")),
    )
    result = handlers_conflict.handle_memory_conflict_question(job)
    assert result.outcome == HandlerOutcome.COMPLETED
    assert str(result.reason).startswith(BLOCKED_REASON)


def test_pg_memory_projection_jobs_blocked(pg_sessionmaker) -> None:
    """Core / Graph / Vector 投影：Memory 被 Shield 后不得重建。"""
    from aiive.db.models import MemoryRecord
    from aiive.worker import outbox_handlers

    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        memory = MemoryRecord(
            id=_id(), memory_type="fact", canonical_key="k:v626pj",
            scope_type="thread", scope_id=thread_id, content="old",
            lifecycle_state="active", validity_state="valid", confidence=0.9,
            created_at=_now(),
        )
        s.add(memory)
        s.commit()
        _add_shield(s, target_type="memory_record", target_id=memory.id)
        jobs = {
            "memory_vector_refresh": _running_job(
                s, job_type="memory_vector_refresh",
                payload={"memory_id": memory.id, "record_version": 1},
            ),
            "core_memory_refresh": _running_job(
                s, job_type="core_memory_refresh",
                payload={"memory_id": memory.id, "record_version": 1},
            ),
            "memory_graph_refresh": _running_job(
                s, job_type="memory_graph_refresh",
                payload={"memory_id": memory.id, "record_version": 1},
            ),
        }
    finally:
        s.close()

    handlers = {
        "memory_vector_refresh": outbox_handlers.handle_memory_vector_refresh,
        "core_memory_refresh": outbox_handlers.handle_core_memory_refresh,
        "memory_graph_refresh": outbox_handlers.handle_memory_graph_refresh,
    }
    for name, job in jobs.items():
        result = handlers[name](job)
        assert result.outcome == HandlerOutcome.COMPLETED, name
        assert str(result.reason).startswith(BLOCKED_REASON), name


def test_pg_tool_operation_blocked_after_forget(pg_sessionmaker) -> None:
    """ToolOperation：Turn 被 Shield 后不得执行旧内容副作用。"""
    from aiive.db.models import ToolOperation
    from aiive.tools import operation_executor

    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        turn = _turn(s, thread_id)
        job = _running_job(s, job_type="tool_operation",
                           payload={"tool_operation_id": "op-v626pg"})
        s.add(ToolOperation(
            id="op-v626pg", idempotency_key="idem-v626pg", thread_id=thread_id,
            turn_record_id=turn.id, turn_id=turn.turn_id, tool_call_id="call-pg",
            capability_id="cap", params={}, params_hash="h", descriptor_hash="d",
            status="queued", effect_mode="db_transactional",
            outbox_job_id=job.id, trace_id="t-v626pg",
        ))
        s.commit()
        _add_shield(s, target_type="turn_record", target_id=turn.id,
                    thread_id=thread_id)
    finally:
        s.close()

    result = operation_executor.handle_tool_operation(job)
    assert result.outcome == HandlerOutcome.COMPLETED
    assert str(result.reason).startswith(BLOCKED_REASON)


def test_pg_claim_fencing_rejections(pg_sessionmaker) -> None:
    """旧 claim、pending、空 token、过期 lease 全部拒绝。"""
    from aiive.worker.outbox_execution_auth import refresh_claimed_job

    s: Session = pg_sessionmaker()
    try:
        job = _running_job(s, job_type="goal_event_dispatch",
                           payload={"event_id": _id()})
        fresh, reason = refresh_claimed_job(job, pg_sessionmaker)
        assert fresh is not None and reason == ""

        def _mutate(**changes):
            s.query(OutboxJob).filter(OutboxJob.id == job.id).update(changes)
            s.commit()

        _mutate(status="pending")
        assert refresh_claimed_job(job, pg_sessionmaker)[0] is None

        _mutate(status="running", claim_token="")
        assert refresh_claimed_job(job, pg_sessionmaker)[0] is None

        _mutate(claim_token=job.claim_token, locked_by="other-worker")
        assert refresh_claimed_job(job, pg_sessionmaker)[0] is None

        _mutate(locked_by=job.worker_id,
                lease_expires_at=_now() - timedelta(seconds=5))
        assert refresh_claimed_job(job, pg_sessionmaker)[0] is None

        _mutate(lease_expires_at=_now() + timedelta(minutes=5))
        assert refresh_claimed_job(job, pg_sessionmaker)[0] is not None
    finally:
        s.close()


def test_pg_unlock_failure_invalidates_connection() -> None:
    """unlock 返回 false 或抛异常时必须 invalidate，禁止带锁归还连接池。"""
    from aiive.worker.outbox_execution_auth import (
        _AuthorizationState,
        _release_shared,
    )

    class _Conn:
        def __init__(self, granted, raise_on_unlock=False):
            self._granted = granted
            self._raise = raise_on_unlock
            self.invalidated = False
            self.closed = False

        def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003
            if self._raise:
                raise RuntimeError("unlock boom")
            outer = self

            class _R:
                def scalar(self):
                    return outer._granted

            return _R()

        def commit(self) -> None:
            return None

        def invalidate(self) -> None:
            self.invalidated = True

        def close(self) -> None:
            self.closed = True

    for granted, raise_on_unlock in ((False, False), (True, True)):
        conn = _Conn(granted, raise_on_unlock)
        state = _AuthorizationState(connection=conn)
        _release_shared(state)
        assert state.connection is None
        assert conn.invalidated is True
        assert conn.closed is True


def test_pg_new_job_not_blocked(pg_sessionmaker) -> None:
    """Shield 之后产生的新 Event / 新 Job 不被误拦截。"""
    from aiive.worker.job_source_guard import blocked_source_reason, resolve_job_sources

    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        turn = _turn(s, thread_id)
        old_ev = _event(s, thread_id, "old", turn.turn_id)
        _add_shield(s, target_type="event", target_id=old_ev.id, thread_id=thread_id)
        new_ev = _event(s, thread_id, "new", turn.turn_id)
        job = _running_job(s, job_type="goal_event_dispatch",
                           payload={"event_id": new_ev.id})
        sources = resolve_job_sources(s, job)
        assert blocked_source_reason(s, job, sources) == ""
    finally:
        s.close()


def test_pg_reminder_blocked_then_allowed(pg_sessionmaker, monkeypatch) -> None:
    """Reminder：来源 Event 被 Shield 时拦截；来源恢复后放行到 Turn 执行。"""
    from aiive.db.models import Task
    from aiive.worker import outbox_handlers

    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        turn = _turn(s, thread_id)
        ev = _event(s, thread_id, "secret", turn.turn_id)
        task = Task(
            id=_id(), task_type="reminder", status="dispatching",
            title="喝水", source_event_id=ev.id, thread_id=thread_id,
        )
        s.add(task)
        s.commit()
        _add_shield(s, target_type="event", target_id=ev.id, thread_id=thread_id)
        job = _running_job(s, job_type="reminder_delivery", payload={
            "task_id": task.id, "thread_id": thread_id, "event_id": ev.id,
            "title": "喝水", "content": "该喝水了",
        })
    finally:
        s.close()

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("Agent Turn 不得启动")

    monkeypatch.setattr(outbox_handlers, "SessionLocal", pg_sessionmaker)
    monkeypatch.setattr(
        "aiive.runtime.turn_execution.TurnExecutionService.execute_turn", _boom,
    )
    blocked = outbox_handlers.handle_reminder_delivery(job)
    assert blocked.outcome == HandlerOutcome.COMPLETED
    assert str(blocked.reason).startswith(BLOCKED_REASON)

    s2: Session = pg_sessionmaker()
    try:
        _retire(s2, "event", ev.id)
    finally:
        s2.close()

    allowed = outbox_handlers.handle_reminder_delivery(job)
    assert not str(allowed.reason).startswith(BLOCKED_REASON)


def test_pg_agent_task_blocked_then_allowed(pg_sessionmaker, monkeypatch) -> None:
    """Agent Task：来源 Turn 被 Shield 时拦截；恢复后进入运行时。"""
    from aiive.db.models import AgentTask
    from aiive.worker import handlers_agent_tasks

    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        turn = _turn(s, thread_id)
        ev = _event(s, thread_id, "secret", turn.turn_id)
        task = AgentTask(
            id=_id(), thread_id=thread_id, source_turn_record_id=turn.id,
            task_type="research", title="t", goal="g", status="queued",
            executor_type="llm", priority=0, task_brief={}, task_state={},
            created_at=_now(),
        )
        s.add(task)
        s.commit()
        _add_shield(s, target_type="turn_record", target_id=turn.id,
                    thread_id=thread_id)
        job = _running_job(s, job_type="agent_task_run",
                           payload={"task_id": task.id})
        assert ev is not None
    finally:
        s.close()

    calls: list[str] = []

    def _run(self, task_id, *, trigger="dispatch"):  # noqa: ANN001, ANN003
        calls.append(task_id)
        raise RuntimeError("runtime reached")

    monkeypatch.setattr(handlers_agent_tasks, "SessionLocal", pg_sessionmaker)
    monkeypatch.setattr(handlers_agent_tasks.PersistentTaskRuntime, "run", _run)
    blocked = handlers_agent_tasks.handle_agent_task_run(job)
    assert str(blocked.reason).startswith(BLOCKED_REASON)
    assert calls == []

    s2: Session = pg_sessionmaker()
    try:
        _retire(s2, "turn_record", turn.id)
    finally:
        s2.close()

    handlers_agent_tasks.handle_agent_task_run(job)
    assert calls == [task.id]