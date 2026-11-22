"""Phase 4 调度与 Worker 链路测试（集成 + 单元）。

覆盖测试矩阵：
- #1 Daily 24h due 触发 enqueue
- #2 idle 线程触发 enqueue
- #3 无 dirty memory 不创建空任务
- #4 operation_id 幂等（UNIQUE 生效）
- #48 operation_id 稳定格式 / Run scope 不混淆 MemoryRecord.scope
- #39 run_memory_maintenance 工具真正 enqueue
- #5 claim / takeover / fencing：旧 token 无法提交
- #20 deadletter 时 Run + OutboxJob + Batch 原子终态；旧 claim 不得 deadletter
- #42 CONTINUE：Job 重新 pending、清 claim、Run 保持 running、不增失败计数
- #62 CONTINUE 不计 failure_attempt_count
- #63 Batch deadletter 终态一致（status=deadletter，不混用 aborted）
- #30 截断后续跑处理完剩余 dirty（双 lane CONTINUE）
"""
from __future__ import annotations

from tests.tool_helpers import invoke_test_tool, tool_method

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from aiive.db.base import SessionLocal
from aiive.db.forget_models import ContentProvenanceRef
from aiive.forget.phase_a_shield import execute_phase_a_shield
from aiive.db.models import (
    Event,
    MemoryMaintenanceAction,
    MemoryMaintenanceBatch,
    MemoryMaintenanceInput,
    MemoryMaintenanceRun,
    MemoryRecord,
    OutboxJob,
    Thread,
)
from aiive.memory.memory_types import LifecycleState
from aiive.context.run_context import RunContext
from aiive.tools.builtin_tools import RunMemoryMaintenanceTool as _handle_run_memory_maintenance
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome
from aiive.worker.handler_registry import HandlerRegistry
from aiive.worker.outbox_handlers import handle_memory_maintenance, register_all
from aiive.worker.outbox_heartbeat import ActiveClaimRegistry
from aiive.runtime.thread_state import ThreadState
from aiive.worker.outbox_worker import OutboxWorker
from aiive.worker.scheduler_daemon import (
    _maintenance_scanner_job,
    enqueue_maintenance_job,
    set_outbox_worker,
)
from tests.support import ensure_running_claim


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _scheduler_fallback():
    """隔离：所有 scheduler 测试走 fallback 直插路径（不依赖已注册 OutboxWorker）。"""
    import aiive.worker.scheduler_daemon as sd

    prev = sd._outbox_worker
    sd._outbox_worker = None
    yield
    sd._outbox_worker = prev


def _mk_record(db, **kwargs) -> MemoryRecord:
    defaults = dict(
        id=str(uuid.uuid4()),
        memory_type="fact",
        canonical_key="test.key",
        scope_type="global",
        scope_id=None,
        content="content",
        lifecycle_state=LifecycleState.CANDIDATE.value,
        validity_state="valid",
        confidence=0.5,
        importance=0.5,
        retention_policy="normal",
        record_version=1,
        pinned=False,
        stability="contextual",
        observed_at=_now(),
        created_at=_now(),
    )
    defaults.update(kwargs)
    rec = MemoryRecord(**defaults)
    db.add(rec)
    db.flush()
    from tests.support import attach_memory_event_source
    attach_memory_event_source(db, rec)
    return rec


def _make_worker() -> OutboxWorker:
    """最小 OutboxWorker 实例：_continue_later / _deadletter 仅依赖 SessionLocal。"""

    class _Stub:
        pass

    return OutboxWorker(worker_id="test-worker", registry=_Stub(), claims=_Stub())


def _make_running_job(db, token, op_id=None, retry_count=0):
    op_id = op_id or f"memory_maintenance:all_user_memories:phase4.v1:{uuid.uuid4()}"
    job = OutboxJob(
        id=str(uuid.uuid4()),
        operation_id=op_id,
        job_type="memory_maintenance",
        status="running",
        payload={"schema_version": 1, "operation_id": op_id},
        max_retries=3,
        retry_count=retry_count,
        claim_token=token,
        locked_by="w1",
        lease_expires_at=_now() + timedelta(seconds=120),
    )
    db.add(job)
    db.commit()
    return job, op_id


def _claimed(job, token) -> ClaimedJob:
    claimed = ClaimedJob(
        id=job.id,
        job_type="memory_maintenance",
        payload={"schema_version": 1, "operation_id": job.operation_id},
        trace_id="t",
        retry_count=job.retry_count,
        max_retries=3,
        claim_token=token,
        schema_version=1,
        worker_id="w1",
    )
    return ensure_running_claim(claimed)


# ───────────────────────── Scheduler / 工具入口 ─────────────────────────


def test_scheduler_daily_due_enqueues_job(db):
    """#1 超过 24h 且有 dirty → enqueue maintenance OutboxJob。"""
    _mk_record(db, lifecycle_state=LifecycleState.ACTIVE.value)
    db.commit()
    _maintenance_scanner_job()
    jobs = db.query(OutboxJob).filter(OutboxJob.job_type == "memory_maintenance").all()
    assert len(jobs) == 1
    assert jobs[0].status == "pending"


def test_scheduler_no_dirty_no_job(db):
    """#3 无 dirty memory 不创建空任务（不建 Run/Job）。"""
    db.commit()
    _maintenance_scanner_job()
    assert db.query(OutboxJob).filter(OutboxJob.job_type == "memory_maintenance").count() == 0
    assert db.query(MemoryMaintenanceRun).count() == 0


def test_scheduler_idempotent_operation_id(db):
    """#4 重复 enqueue 同 window_bucket 只产生一个 Job（UNIQUE 生效）。"""
    op = enqueue_maintenance_job(db, window_bucket="b1", now=_now())
    op2 = enqueue_maintenance_job(db, window_bucket="b1", now=_now())
    db.commit()
    assert op == op2
    assert db.query(OutboxJob).filter(OutboxJob.operation_id == op).count() == 1


@pytest.fixture
def registered_worker(_scheduler_fallback):
    registry = HandlerRegistry()
    register_all(registry)
    worker = OutboxWorker("maintenance-test", registry, ActiveClaimRegistry())
    set_outbox_worker(worker)
    return worker


@pytest.mark.parametrize("status", ["pending", "running", "completed", "deadletter"])
def test_production_path_idempotent_with_worker(db, registered_worker, status):
    now = _now()
    op_id = enqueue_maintenance_job(db, now=now)
    assert enqueue_maintenance_job(db, now=now) == op_id
    job = db.query(OutboxJob).filter_by(operation_id=op_id).one()
    job.status = status
    db.commit()

    assert enqueue_maintenance_job(db, now=now) == op_id
    next_op = enqueue_maintenance_job(db, now=now + timedelta(days=1))
    db.commit()
    assert db.query(OutboxJob).filter_by(operation_id=op_id).one().status == status
    if status in {"pending", "running"}:
        assert next_op is None
    else:
        assert next_op and next_op != op_id
        assert db.query(OutboxJob).filter_by(operation_id=next_op).one().status == "pending"


def test_manual_attempt_has_own_audit_and_retry_budget(db, registered_worker):
    now = _now()
    base_id = enqueue_maintenance_job(db, now=now)
    base = db.query(OutboxJob).filter_by(operation_id=base_id).one()
    base.status = "deadletter"
    base.retry_count = base.max_retries
    base.terminal_at = now
    db.commit()

    run_manually = tool_method(_handle_run_memory_maintenance)
    ctx = RunContext(thread_id="manual-thread", turn_id="manual-1",
                     turn_record_id="manual-attempt-1", trace_id="manual-trace")
    first = run_manually(db, ctx)
    manual_id = first["maintenance_operation_id"]
    assert first["enqueued"] and manual_id != base_id
    assert manual_id.endswith(":manual-manual-attempt-1")
    assert run_manually(db, ctx)["maintenance_operation_id"] == manual_id
    manual = db.query(OutboxJob).filter_by(operation_id=manual_id).one()
    assert manual.retry_count == 0
    assert manual.max_retries == 3
    manual.status = "deadletter"
    db.commit()
    assert run_manually(db, ctx)["maintenance_operation_id"] == manual_id
    assert manual.status == "deadletter"
    second = run_manually(db, RunContext(thread_id="manual-thread", turn_id="manual-2",
                                       turn_record_id="manual-attempt-2", trace_id="manual-trace-2"))
    assert second["enqueued"] and second["maintenance_operation_id"] not in (base_id, manual_id)
    db.commit()
    assert base.status == "deadletter" and base.retry_count == base.max_retries
    assert base.terminal_at.replace(tzinfo=timezone.utc) == now


def test_scheduler_operation_id_format(db):
    """#48 operation_id 稳定格式，且 Run scope 不混淆 MemoryRecord.scope。"""
    op = enqueue_maintenance_job(db, window_bucket="2026-07-15", now=_now())
    assert op == "memory_maintenance:all_user_memories:phase4.v1:2026-07-15"


def test_scheduler_idle_triggers(db, registered_worker):
    """#2 Daily 未到期时，Idle 也能创建当天的自动维护任务。"""
    now = _now()
    # 已有近期的 succeeded run，使 Daily 条件不满足（daily_due=False），逼出 idle 分支
    old_job = OutboxJob(
        id=str(uuid.uuid4()), operation_id="old-job", job_type="memory_maintenance",
        status="completed", payload={}, max_retries=3,
    )
    db.add(old_job)
    db.flush()
    db.add(MemoryMaintenanceRun(
        id=str(uuid.uuid4()), outbox_job_id=old_job.id,
        operation_id="old-run", scope_type="all_user_memories",
        cutoff_updated_at=now - timedelta(minutes=3), status="succeeded", completed_at=now,
    ))
    _mk_record(db, lifecycle_state=LifecycleState.ACTIVE.value, updated_at=now - timedelta(minutes=1))
    db.add(Thread(id="t1", last_activity_at=now - timedelta(seconds=1000)))
    db.commit()
    _maintenance_scanner_job()
    jobs = db.query(OutboxJob).filter(OutboxJob.job_type == "memory_maintenance").all()
    assert len(jobs) == 2
    auto = next(job for job in jobs if job.operation_id != "old-job")
    assert auto.operation_id.endswith(now.strftime("%Y-%m-%d"))
    auto.status = "deadletter"
    db.commit()
    _maintenance_scanner_job()
    assert db.query(OutboxJob).filter_by(job_type="memory_maintenance").count() == 2


@pytest.mark.parametrize("daily_due", [True, False])
def test_scanner_skips_when_manual_maintenance_is_running(db, registered_worker, daily_due):
    now = _now()
    if not daily_due:
        old = OutboxJob(operation_id="prior", job_type="memory_maintenance", status="completed", payload={})
        db.add(old)
        db.flush()
        db.add(MemoryMaintenanceRun(
            outbox_job_id=old.id, operation_id=old.operation_id,
            cutoff_updated_at=now - timedelta(minutes=1), status="succeeded", completed_at=now,
        ))
    _mk_record(db, lifecycle_state="active")
    db.add(Thread(id="idle-thread", last_activity_at=now - timedelta(days=1)))
    manual_id = enqueue_maintenance_job(db, window_bucket="manual-active", now=now)
    manual = db.query(OutboxJob).filter_by(operation_id=manual_id).one()
    manual.status = "running"
    db.commit()

    _maintenance_scanner_job()
    assert db.query(OutboxJob).filter(OutboxJob.operation_id.like("memory_maintenance:%")).count() == 1


def test_scanner_reuses_legacy_idle_daily_window(db, registered_worker):
    now = _now()
    legacy_id = enqueue_maintenance_job(db, window_bucket=f"idle-{now:%Y-%m-%d}", now=now)
    db.query(OutboxJob).filter_by(operation_id=legacy_id).one().status = "deadletter"
    _mk_record(db, lifecycle_state="active")
    db.commit()

    _maintenance_scanner_job()
    assert db.query(OutboxJob).filter_by(job_type="memory_maintenance").one().operation_id == legacy_id


def test_tool_run_memory_maintenance_enqueues(db):
    """#39 工具真正 enqueue Job（而非仅返回统计）。"""
    _mk_record(db, lifecycle_state=LifecycleState.ACTIVE.value)
    db.commit()
    ctx = RunContext(
        thread_id="t-maintenance",
        trace_id="trace-maintenance",
        turn_id="turn-maintenance",
        turn_record_id="turn-record-maintenance",
    )
    original = tool_method(_handle_run_memory_maintenance)
    result = original(db, ctx)
    assert result["enqueued"] is True
    assert result["maintenance_operation_id"] is not None
    assert result["pre_enqueue_scan"]["total_scanned"] == 1
    job = db.query(OutboxJob).filter(
        OutboxJob.operation_id == result["maintenance_operation_id"],
    ).one()
    assert job.payload["thread_id"] == "t-maintenance"
    assert job.payload["turn_id"] == "turn-maintenance"
    assert job.payload["trace_id"] == "trace-maintenance"


def test_maintenance_tool_is_registered_as_persistent_side_effect():
    """维护入队必须走 ToolOperation，不能在线程池中假装只读执行。"""
    from aiive.tools.registry import get_tool_registry

    registration = get_tool_registry().get("run_memory_maintenance")
    assert registration is not None
    assert registration.safety.writes_external_world is True
    assert registration.safety.effect_mode == "db_transactional"


# ───────────────────────── Worker：CONTINUE / deadletter / fencing ─────────────────────────


def test_continue_later_repending_and_no_count_increment(db):
    """#42 CONTINUE：Job 重新 pending、清 claim/lease、retry_count 不增。"""
    token = str(uuid.uuid4())
    job, _ = _make_running_job(db, token, retry_count=2)
    _make_worker()._continue_later(_claimed(job, token), "lane_continue")
    s = SessionLocal()
    try:
        j = s.get(OutboxJob, job.id)
        assert j.status == "pending"
        assert j.claim_token is None
        assert j.locked_by is None
        assert j.retry_count == 2
        assert j.available_at is not None
    finally:
        s.close()


def test_continue_later_fencing_old_token(db):
    """#5 旧 token 无法 _continue_later（fencing）。"""
    token = str(uuid.uuid4())
    job, _ = _make_running_job(db, token)
    _make_worker()._continue_later(_claimed(job, "wrong-token"), "lane_continue")
    s = SessionLocal()
    try:
        j = s.get(OutboxJob, job.id)
        assert j.status == "running"
        assert j.claim_token == token
    finally:
        s.close()


def test_deadletter_atomic_run_and_batches(db):
    """#20 #63 deadletter：OutboxJob + Run + 未完成 Batch 原子 deadletter。"""
    token = str(uuid.uuid4())
    job, op_id = _make_running_job(db, token)
    run = MemoryMaintenanceRun(
        id=str(uuid.uuid4()), outbox_job_id=job.id, operation_id=op_id,
        scope_type="all_user_memories", cutoff_updated_at=_now(),
        status="running", execution_token=token,
    )
    db.add(run)
    db.flush()
    batch = MemoryMaintenanceBatch(
        id=str(uuid.uuid4()), run_id=run.id, candidate_lane="changed", status="frozen",
    )
    db.add(batch)
    db.commit()

    _make_worker()._deadletter_job_and_ingestion_run(
        _claimed(job, token), ingestion_run_id=run.id, error="boom", terminal_reason="test",
    )
    s = SessionLocal()
    try:
        assert s.get(OutboxJob, job.id).status == "deadletter"
        assert s.get(MemoryMaintenanceRun, run.id).status == "deadletter"
        assert s.get(MemoryMaintenanceBatch, batch.id).status == "deadletter"
    finally:
        s.close()


def test_maintenance_deadletter_persists_terminal_card_event(db):
    """维护 deadletter 与 failed 卡片事实必须在同一终态事务提交。"""
    token = str(uuid.uuid4())
    job, op_id = _make_running_job(db, token)
    job.payload = {
        "schema_version": 1,
        "operation_id": op_id,
        "thread_id": "maintenance-thread",
        "turn_id": "maintenance-turn",
        "trace_id": "maintenance-trace",
    }
    db.add(Thread(id="maintenance-thread"))
    run = MemoryMaintenanceRun(
        id=str(uuid.uuid4()), outbox_job_id=job.id, operation_id=op_id,
        scope_type="all_user_memories", cutoff_updated_at=_now(),
        status="running", execution_token=token,
    )
    db.add(run)
    db.commit()

    _make_worker()._deadletter_job_and_ingestion_run(
        _claimed(job, token), ingestion_run_id=run.id,
        error="maintenance boom", terminal_reason="test",
    )

    s = SessionLocal()
    try:
        event = s.query(Event).filter(
            Event.event_type == "maintenance_report_terminal",
            Event.turn_id == "maintenance-turn",
        ).one()
        assert event.payload["status"] == "failed"
        assert event.payload["card"]["card_type"] == "maintenance_report"
        assert event.payload["card"]["resource_refs"]["operation_id"] == op_id
        assert event.payload["card"]["payload_preview"]["result"]["error"] == "maintenance boom"
        llm_event = Event(
            id="maintenance-llm-event",
            trace_id="maintenance-trace",
            thread_id="maintenance-thread",
            event_type="llm_response",
            turn_id="maintenance-turn",
            payload={
                "content": "维护已启动",
                "action_cards": [{
                    "card_type": "maintenance_report",
                    "title": "记忆维护已入队",
                    "summary": "后台维护正在执行",
                    "trace_id": "maintenance-trace",
                    "event_ids": [],
                    "resource_refs": {"operation_id": op_id},
                    "status": "pending",
                    "payload_preview": {"pre_enqueue_scan": {"total_scanned": 1}},
                    "reminder_id": "",
                    "actions": [],
                }],
            },
        )
        events = [llm_event, event]
        messages = ThreadState._events_to_display_messages(events)
        card = messages[0]["action_cards"][0]
        assert card["status"] == "failed"
        assert card["payload_preview"]["result"]["error"] == "maintenance boom"
    finally:
        s.close()


def test_deadletter_fencing_old_claim(db):
    """#5 #20 旧 claim 不得 deadletter（fencing：错误 token 调用无变更）。"""
    token = str(uuid.uuid4())
    job, op_id = _make_running_job(db, token)
    run = MemoryMaintenanceRun(
        id=str(uuid.uuid4()), outbox_job_id=job.id, operation_id=op_id,
        scope_type="all_user_memories", cutoff_updated_at=_now(),
        status="running", execution_token=token,
    )
    db.add(run)
    db.commit()

    _make_worker()._deadletter_job_and_ingestion_run(
        _claimed(job, "wrong-token"), ingestion_run_id=run.id, error="boom", terminal_reason="test",
    )
    s = SessionLocal()
    try:
        assert s.get(OutboxJob, job.id).status == "running"
        assert s.get(MemoryMaintenanceRun, run.id).status == "running"
    finally:
        s.close()


# ───────────────────────── Handler 端到端 CONTINUE（#30 #42）─────────────────────────


def _claim_once(db, job_id, op_id) -> HandlerOutcome:
    """单轮 claim + dispatch（绕过 Worker 循环，便于断言中间态）。"""
    s = SessionLocal()
    try:
        job = s.query(OutboxJob).filter(OutboxJob.id == job_id).with_for_update().first()
        token = str(uuid.uuid4())
        job.status = "running"
        job.claim_token = token
        job.locked_by = "test-worker"
        job.lease_expires_at = _now() + timedelta(seconds=120)
        s.commit()
    finally:
        s.close()
    claimed = ClaimedJob(
        id=job_id, job_type="memory_maintenance",
        payload={"schema_version": 1, "operation_id": op_id},
        trace_id="t", retry_count=0, max_retries=3,
        claim_token=token, schema_version=1, worker_id="test-worker",
    )
    return handle_memory_maintenance(claimed).outcome


def test_handler_continue_keeps_run_running(db, monkeypatch):
    """#30 #42 单 batch 截断：两条同 lane 候选分两轮处理，首轮 CONTINUE 时 Run 仍
    running、不计失败；续跑后 COMPLETED，记录不物理删除。"""
    import aiive.memory.recall_config as rc

    # 缩小单批上限，使 2 条候选无法在一轮内处理完 → 必触发 CONTINUE
    orig = rc.MaintenanceConfig
    monkeypatch.setattr(rc, "MaintenanceConfig", lambda: orig(max_maintenance_batch=1))

    now = _now()
    # 两条 fresh candidate 同属 changed lane（confidence<0.7 不 promote）
    r1 = _mk_record(db, lifecycle_state=LifecycleState.CANDIDATE.value, confidence=0.5)
    r2 = _mk_record(db, lifecycle_state=LifecycleState.CANDIDATE.value, confidence=0.5)
    db.commit()

    op_id = f"memory_maintenance:all_user_memories:phase4.v1:{now.strftime('%Y-%m-%d')}"
    job = OutboxJob(
        operation_id=op_id, job_type="memory_maintenance",
        status="pending", payload={"schema_version": 1}, max_retries=3,
    )
    db.add(job)
    db.commit()
    job_id = job.id

    # 首轮：单 batch 仅含 1 条 seed → 剩余 1 条 → CONTINUE
    first = _claim_once(db, job_id, op_id)
    assert first == HandlerOutcome.CONTINUE

    s = SessionLocal()
    try:
        run = s.query(MemoryMaintenanceRun).filter(
            MemoryMaintenanceRun.outbox_job_id == job_id).first()
        assert run is not None
        assert run.scope_type == "all_user_memories"  # #48：不混淆 MemoryRecord.scope
        assert run.status == "running"
        assert run.failure_attempt_count == 0  # #62：CONTINUE 不计失败
    finally:
        s.close()

    # 续跑直到终态
    outcome = None
    for _ in range(10):
        o = _claim_once(db, job_id, op_id)
        if o in (HandlerOutcome.COMPLETED, HandlerOutcome.NON_RETRYABLE, HandlerOutcome.CLAIM_LOST):
            outcome = o
            break
    assert outcome == HandlerOutcome.COMPLETED

    s = SessionLocal()
    try:
        # #26 不物理删除：两条记录仍存在（维护动作不改变其存在性）
        assert s.get(MemoryRecord, r1.id) is not None
        assert s.get(MemoryRecord, r2.id) is not None
        run = s.query(MemoryMaintenanceRun).filter(
            MemoryMaintenanceRun.outbox_job_id == job_id).first()
        assert run.status == "succeeded"
    finally:
        s.close()


@pytest.mark.parametrize("lane", ["changed", "expired_ephemeral", "candidate_due", "sleep_due"])
def test_handler_resumes_time_only_cursor_at_timestamp_boundary(db, registered_worker, monkeypatch, lane):
    import aiive.memory.memory_maintenance_planner as planner
    import aiive.memory.recall_config as rc

    config = rc.MaintenanceConfig(max_maintenance_batch=1)
    monkeypatch.setattr(rc, "MaintenanceConfig", lambda: config)
    now = _now()
    types = frozenset({"memory_maintenance"})
    assert enqueue_maintenance_job(db, window_bucket="before-boundary")
    db.commit()
    assert registered_worker.poll(max_jobs=1, only_types=types) == 1
    previous = db.query(MemoryMaintenanceRun).one()
    assert previous.status == "succeeded"
    assert (previous.cursor_updated_at, previous.cursor_id) == (None, None)

    boundary = previous.cutoff_updated_at
    if lane != "changed":
        boundary = boundary - timedelta(days=90)
    updated_at = previous.cutoff_updated_at if lane == "changed" else boundary - timedelta(days=1)
    settings = {
        "changed": {},
        "expired_ephemeral": dict(lifecycle_state="active", retention_policy="ephemeral", valid_to=boundary),
        "candidate_due": dict(created_at=boundary),
        "sleep_due": dict(lifecycle_state="active", last_accessed_at=boundary),
    }[lane]
    ids = ["boundary-a", "boundary-b", "boundary-c"]
    hidden_id = "boundary-ax"
    for record_id in [*ids, hidden_id]:
        _mk_record(db, id=record_id, canonical_key=record_id, **settings)
    db.flush()
    db.query(MemoryRecord).filter(MemoryRecord.id.in_([*ids, hidden_id])).update({"updated_at": updated_at})
    source_ref = db.query(ContentProvenanceRef).filter_by(
        owner_type="memory_record", owner_id=hidden_id, source_type="event",
    ).one()
    execute_phase_a_shield(db, mode="history_only", event_ids=[source_ref.source_id], reason="cursor regression")

    op_id = enqueue_maintenance_job(db, window_bucket="after-boundary", now=now)
    job = db.query(OutboxJob).filter_by(operation_id=op_id).one()

    if lane != "changed":
        # A persisted legacy Batch can have a time-only end cursor. The real
        # handler recovers that cursor through _maintenance_lane_cursor.
        run = MemoryMaintenanceRun(
            outbox_job_id=job.id, operation_id=op_id, cutoff_updated_at=now + timedelta(seconds=1),
            cursor_updated_at=previous.cutoff_updated_at, status="running",
        )
        db.add(run)
        db.flush()
        db.add(MemoryMaintenanceBatch(
            run_id=run.id, batch_no=0, candidate_lane=lane, status="done", cursor_end_time=boundary,
        ))
    db.commit()

    class WorkerInterrupted(BaseException):
        pass

    def stop_before_planning(*args, **kwargs):
        raise WorkerInterrupted

    with monkeypatch.context() as interrupted:
        interrupted.setattr(planner, "plan_batch", stop_before_planning)
        with pytest.raises(WorkerInterrupted):
            registered_worker.poll(max_jobs=1, only_types=types)
    db.expire_all()
    run = db.query(MemoryMaintenanceRun).filter_by(outbox_job_id=job.id).one()
    frozen = db.query(MemoryMaintenanceBatch).filter_by(run_id=run.id, status="frozen").one()
    assert frozen.candidate_lane == lane
    assert frozen.cursor_start_time == boundary
    assert frozen.cursor_start_id is None
    assert frozen.cursor_end_id == ids[0]
    frozen_id = frozen.id
    first_input = db.query(MemoryMaintenanceInput).filter_by(batch_id=frozen_id).one()
    assert first_input.memory_record_id == ids[0]
    first_input_id = first_input.id

    job.lease_expires_at = now - timedelta(seconds=1)
    db.commit()
    assert registered_worker.poll(max_jobs=1, only_types=types) == 1
    db.expire_all()
    assert db.get(MemoryMaintenanceBatch, frozen_id).status == "done"
    assert db.query(MemoryMaintenanceInput).filter_by(batch_id=frozen_id).one().id == first_input_id
    assert job.status == "pending"

    for _ in range(10):
        if job.status == "completed":
            break
        assert registered_worker.poll(max_jobs=1, only_types=types) == 1
        db.expire_all()
    assert job.status == "completed"
    assert job.retry_count == run.failure_attempt_count == 0
    assert run.status == "succeeded"
    batches = db.query(MemoryMaintenanceBatch).filter(
        MemoryMaintenanceBatch.run_id == run.id,
        MemoryMaintenanceBatch.candidate_lane == lane,
        MemoryMaintenanceBatch.cursor_end_id.isnot(None),
    ).order_by(MemoryMaintenanceBatch.batch_no).all()
    scanned_ids = [ids[0], hidden_id, *ids[1:]]
    assert [batch.cursor_end_id for batch in batches] == scanned_ids
    assert [batch.cursor_start_id for batch in batches] == [None, *scanned_ids[:-1]]
    skipped = db.query(MemoryMaintenanceAction).filter_by(
        run_id=run.id, subject_memory_record_id=hidden_id, reason_code="forget_source_blocked",
    ).one()
    assert skipped.status == "skipped_stale"
    seed_ids = [
        row.memory_record_id
        for batch in batches
        for row in db.query(MemoryMaintenanceInput).filter_by(batch_id=batch.id, input_role="seed")
    ]
    assert seed_ids == ids
