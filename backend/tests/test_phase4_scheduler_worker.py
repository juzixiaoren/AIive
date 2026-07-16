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

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from aiive.db.base import SessionLocal
from aiive.db.models import (
    MemoryMaintenanceBatch,
    MemoryMaintenanceRun,
    MemoryRecord,
    OutboxJob,
    Thread,
)
from aiive.memory.memory_types import LifecycleState
from aiive.tools.builtin_tools import _handle_run_memory_maintenance
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome
from aiive.worker.outbox_handlers import handle_memory_maintenance
from aiive.worker.outbox_worker import OutboxWorker
from aiive.worker.scheduler_daemon import (
    _maintenance_scanner_job,
    enqueue_maintenance_job,
)


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
        payload={"schema_version": 1},
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
    return ClaimedJob(
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


def test_production_path_idempotent_with_worker(db):
    """🔴 回归：生产态 _outbox_worker 已注册时，同 window_bucket 仍幂等，
    不会因 OutboxWorker.enqueue 裸 db.add 触发 operation_id UNIQUE 冲突。

    复现原 bug：若去重检查被跳过，第二次 enqueue 会 db.add 同 operation_id，
    commit 时抛 IntegrityError。
    """
    import aiive.worker.scheduler_daemon as sd

    class _StubWorker:
        def enqueue(self, db, job_type, payload, trace_id=None, operation_id=None):
            if job_type != "memory_maintenance":
                raise ValueError("not allowed")
            db.add(OutboxJob(
                operation_id=operation_id, job_type=job_type,
                status="pending", payload=payload, max_retries=3,
            ))
            return None

    prev = sd._outbox_worker
    sd._outbox_worker = _StubWorker()
    try:
        op1 = enqueue_maintenance_job(db, window_bucket="b-prod", now=_now())
        op2 = enqueue_maintenance_job(db, window_bucket="b-prod", now=_now())
        db.commit()
    finally:
        sd._outbox_worker = prev
    assert op1 == op2
    assert db.query(OutboxJob).filter(OutboxJob.operation_id == op1).count() == 1


def test_scheduler_operation_id_format(db):
    """#48 operation_id 稳定格式，且 Run scope 不混淆 MemoryRecord.scope。"""
    op = enqueue_maintenance_job(db, window_bucket="2026-07-15", now=_now())
    assert op == "memory_maintenance:all_user_memories:phase4.v1:2026-07-15"


def test_scheduler_idle_triggers(db):
    """#2 idle 线程 + 维护后 dirty → idle bucket enqueue。"""
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
        cutoff_updated_at=now, status="succeeded", completed_at=now,
    ))
    _mk_record(db, lifecycle_state=LifecycleState.ACTIVE.value)
    db.add(Thread(id="t1", last_activity_at=now - timedelta(seconds=1000)))
    db.commit()
    _maintenance_scanner_job()
    jobs = db.query(OutboxJob).filter(OutboxJob.job_type == "memory_maintenance").all()
    # 既有 old-job 与新 idle bucket 各一条；idle 分支触发后应有含 idle- 前缀的 Job
    assert any("idle-" in j.operation_id for j in jobs)


def test_tool_run_memory_maintenance_enqueues(db):
    """#39 工具真正 enqueue Job（而非仅返回统计）。"""
    _mk_record(db, lifecycle_state=LifecycleState.ACTIVE.value)
    db.commit()
    result = _handle_run_memory_maintenance(db)
    assert result["enqueued"] is True
    assert result["operation_id"] is not None
    assert db.query(OutboxJob).filter(
        OutboxJob.operation_id == result["operation_id"]).count() == 1


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
