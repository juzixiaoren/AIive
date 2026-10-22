"""PostgreSQL 核心契约测试：事务 / 锁 / 幂等 / 并发语义。

这些用例只在真实 PostgreSQL 上运行（SQLite 无法验证 advisory lock、SKIP LOCKED、
FOR UPDATE 语义）。默认快速测试不启用；设置 ``AIIVE_PG_TESTS=1`` 并提供 PG 环境
（Docker 自动拉起，或 ``AIIVE_PG_TEST_URL``）后运行。

覆盖：
  1. 并发写同一记忆键 → advisory lock 串行化，最终只有一条 active+valid 记录，
     且同一 idempotency_key 只落一条 proposal。
  2. 多 Worker 并发 claim → SKIP LOCKED 保证不重复领取。
  3. SKIP LOCKED 跳过已被其他事务锁定的行（不阻塞）。
  4. Forget cascade 阶段异常 → 事务回滚，无半完成 batch / 无 next-stage job。
  5. 事务回滚 → 无残留 MemoryRecord 与不一致的投影 OutboxJob。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from aiive.context.run_context import RunContext
from aiive.db.forget_models import (
    ForgetBatch,
    ForgetOperation,
    ForgetSelectorManifest,
    ForgetStageRun,
)
from aiive.db.models import MemoryProposal as MemoryProposalModel
from aiive.db.models import MemoryRecord, OutboxJob, Thread
from aiive.memory.memory_types import (
    EvidenceItem,
    LifecycleState,
    MemoryProposal,
    TrustLevel,
    ValidityState,
)
from aiive.memory.memory_write_service import MemoryWriteService
from aiive.worker import handlers_forget as hf
from aiive.worker.handler_registry import HandlerRegistry
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome
from aiive.worker.outbox_heartbeat import ActiveClaimRegistry
from aiive.worker.outbox_worker import OutboxWorker
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

pytestmark = pytest.mark.pg


def _proposal(canonical_key: str, content: str) -> MemoryProposal:
    p = MemoryProposal(
        memory_type="user_profile",
        confidence=0.9,
        canonical_key=canonical_key,
        content=content,
        scope_type="global",
        scope_id=None,
        evidence=[EvidenceItem(source_type="user_message", trust_level=TrustLevel.TRUSTED.value)],
    )
    p.compute_request_idempotency()
    return p


def test_pg_schema_is_postgresql(pg_engine):
    """环境自检：确认确实跑在 PostgreSQL 上，且关键表存在。"""
    with pg_engine.connect() as c:
        assert c.execute(text("SHOW server_version")).scalar()
        assert c.execute(text("SELECT to_regclass('public.memory_records') IS NOT NULL")).scalar() is True
        assert c.execute(text("SELECT to_regclass('public.outbox_jobs') IS NOT NULL")).scalar() is True


def test_concurrent_same_key_yields_single_active_record(pg_session, pg_sessionmaker):
    """并发写同一 canonical_key：advisory lock 串行化，最终仅一条 active+valid。"""
    thread_id = "pg-thread-mem"
    pg_session.add(Thread(id=thread_id))
    pg_session.commit()

    key = "pg.contract.concurrent"
    ikey = _proposal(key, "same content").idempotency_key
    n = 8
    barrier = threading.Barrier(n)

    def worker() -> str:
        try:
            barrier.wait(timeout=20)
        except threading.BrokenBarrierError:
            return "ERR:barrier"
        s = pg_sessionmaker()
        try:
            svc = MemoryWriteService(s)
            ctx = RunContext(thread_id=thread_id, trace_id="pg-concurrent", source="user_chat")
            res = svc.write(_proposal(key, "same content"), ctx)
            s.commit()
            return res.outcome.value
        except Exception as exc:  # noqa: BLE001 - 收集并发异常用于断言
            s.rollback()
            return "ERR:" + type(exc).__name__ + ":" + str(exc)[:160]
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(lambda _: worker(), range(n)))

    errors = [r for r in results if r.startswith("ERR")]
    assert not errors, errors

    pg_session.expire_all()
    active = pg_session.query(MemoryRecord).filter(
        MemoryRecord.canonical_key == key,
        MemoryRecord.lifecycle_state == LifecycleState.ACTIVE.value,
        MemoryRecord.validity_state == ValidityState.VALID.value,
    ).all()
    assert len(active) == 1, f"expected 1 active record, got {len(active)}: {[r.id for r in active]}"

    proposals = pg_session.query(MemoryProposalModel).filter(
        MemoryProposalModel.idempotency_key == ikey
    ).all()
    assert len(proposals) == 1, f"expected 1 proposal for idempotency_key, got {len(proposals)}"


def _seed_jobs(pg_session, count: int, prefix: str) -> None:
    now = datetime.now(UTC)
    for i in range(count):
        pg_session.add(OutboxJob(
            id=f"{prefix}-job-{i}",
            operation_id=f"{prefix}-op-{i}",
            job_type="reminder_delivery",
            status="pending",
            payload={},
            created_at=now + timedelta(seconds=i),
        ))
    pg_session.commit()


def test_concurrent_claims_never_duplicate(pg_session, pg_sessionmaker):
    """多 Worker 并发 claim：每个 pending job 恰好被领取一次，无重复。"""
    total = 30
    _seed_jobs(pg_session, total, "claim")
    workers = 6
    barrier = threading.Barrier(workers)

    def worker(wid: str) -> list[str]:
        try:
            barrier.wait(timeout=20)
        except threading.BrokenBarrierError:
            return []
        w = OutboxWorker(
            worker_id=wid,
            registry=HandlerRegistry(),
            claims=ActiveClaimRegistry(),
        )
        got: list[str] = []
        while True:
            claimed = w.claim_one()
            if claimed is None:
                break
            got.append(claimed.id)
        return got

    claimed_all: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for got in ex.map(worker, [f"w{i}" for i in range(workers)]):
            claimed_all.extend(got)

    assert len(claimed_all) == len(set(claimed_all)), "a job was claimed by more than one worker"
    assert len(claimed_all) == total, f"expected {total} claims, got {len(claimed_all)}"


def test_skip_locked_skips_row_locked_by_other_transaction(pg_session, pg_sessionmaker):
    """SKIP LOCKED 语义：第二个事务跳过已被锁定的行，且不阻塞。

    若未使用 SKIP LOCKED，第二个查询会阻塞在行锁上并触发 statement_timeout。
    """
    _seed_jobs(pg_session, 2, "skip")

    s_a = pg_sessionmaker()
    s_b = pg_sessionmaker()
    try:
        row_a = s_a.execute(text(
            "SELECT id FROM outbox_jobs WHERE status='pending' "
            "ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED"
        )).scalar()
        assert row_a == "skip-job-0", row_a

        # s_a 的事务仍持有 row_a 的行锁；s_b 必须跳过它并拿到另一行。
        s_b.execute(text("SET statement_timeout = '5s'"))
        row_b = s_b.execute(text(
            "SELECT id FROM outbox_jobs WHERE status='pending' "
            "ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED"
        )).scalar()
        assert row_b is not None and row_b != row_a, (row_a, row_b)
    finally:
        s_a.rollback()
        s_a.close()
        s_b.rollback()
        s_b.close()


def _seed_forget_operation(pg_session, op_id: str) -> None:
    pg_session.add(ForgetOperation(
        id=op_id,
        operation_key="ok-" + op_id,
        mode="hard",
        selector_type="memory_ids",
        selector_hash="selector-hash",
        status="shielded",
    ))
    pg_session.add(ForgetSelectorManifest(
        id="manifest-" + op_id,
        forget_operation_id=op_id,
        selector_type="memory_ids",
        selector_payload={"selector_type": "memory_ids", "memory_ids": []},
        selector_payload_hash="payload-hash",
        cutoff_created_at=datetime.now(UTC),
    ))
    pg_session.commit()


def test_forget_cascade_exception_leaves_no_half_state(pg_session, monkeypatch):
    """Forget cascade 阶段异常：事务回滚，batch 不进入 done，且不产生 next-stage job。"""
    op_id = "op-forget-pg"
    _seed_forget_operation(pg_session, op_id)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("injected enqueue failure")

    monkeypatch.setattr(hf, "_enqueue_next_stage", _boom)

    claimed = ClaimedJob(
        id="job-cascade-pg",
        job_type="forget_cascade",
        payload={"forget_operation_id": op_id},
        trace_id=None,
        retry_count=0,
        max_retries=3,
        claim_token="tok",
        schema_version=1,
        worker_id="w-pg",
    )
    result = hf.handle_forget_cascade(claimed)
    assert result.outcome == HandlerOutcome.RETRYABLE_ERROR, result

    pg_session.expire_all()
    batch = pg_session.query(ForgetBatch).filter_by(
        forget_operation_id=op_id, stage="cascade"
    ).first()
    assert batch is None or batch.status != "done", "batch must not be marked done after rollback"

    next_jobs = pg_session.query(OutboxJob).filter(
        OutboxJob.job_type == "forget_rebuild_dependencies"
    ).all()
    assert next_jobs == [], "no next-stage job may survive a rolled-back cascade"


def test_rollback_leaves_no_memory_or_projection_residue(pg_session, pg_sessionmaker):
    """事务回滚后：不残留 MemoryRecord，也不残留已入队的投影 OutboxJob。"""
    thread_id = "pg-thread-rollback"
    pg_session.add(Thread(id=thread_id))
    pg_session.commit()

    key = "pg.contract.rollback"
    s = pg_sessionmaker()
    rec_id = ""
    try:
        svc = MemoryWriteService(s)
        ctx = RunContext(thread_id=thread_id, trace_id="pg-rollback", source="user_chat")
        res = svc.write(_proposal(key, "rollback content"), ctx)
        assert res.written is True, res.to_dict()
        s.flush()

        records = s.query(MemoryRecord).filter(MemoryRecord.canonical_key == key).all()
        assert len(records) == 1
        rec_id = records[0].id

        jobs = s.query(OutboxJob).filter(
            OutboxJob.operation_id.like(f"memory_file_projection:{rec_id}:%")
        ).all()
        assert len(jobs) == 1, "projection job must be enqueued in the same transaction"

        s.rollback()
    finally:
        s.close()

    pg_session.expire_all()
    assert pg_session.query(MemoryRecord).filter(MemoryRecord.canonical_key == key).count() == 0
    assert pg_session.query(OutboxJob).filter(
        OutboxJob.operation_id.like(f"memory_file_projection:{rec_id}:%")
    ).count() == 0


def test_forget_deadletter_is_atomic_across_job_stage_operation(pg_session):
    """Forget 阶段耗尽重试进入 deadletter：OutboxJob / ForgetStageRun / ForgetOperation 原子一致。"""
    op_id = "op-forget-dl"
    job_id = "job-forget-dl"
    pg_session.add(ForgetOperation(
        id=op_id,
        operation_key="ok-" + op_id,
        mode="hard",
        selector_type="memory_ids",
        selector_hash="selector-hash",
        status="shielded",
    ))
    pg_session.add(OutboxJob(
        id=job_id,
        operation_id="forget:" + op_id + ":rebuild_dependencies",
        job_type="forget_rebuild_dependencies",
        status="running",
        payload={"forget_operation_id": op_id},
        locked_by="w-dl",
        claim_token="tok-dl",
        retry_count=2,
        max_retries=3,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    ))
    pg_session.flush()
    pg_session.add(ForgetStageRun(
        id="srun-dl",
        forget_operation_id=op_id,
        stage="rebuild_dependencies",
        outbox_job_id=job_id,
        status="running",
        failure_count=0,
    ))
    pg_session.commit()

    claimed = ClaimedJob(
        id=job_id,
        job_type="forget_rebuild_dependencies",
        payload={"forget_operation_id": op_id},
        trace_id=None,
        retry_count=2,
        max_retries=3,
        claim_token="tok-dl",
        schema_version=1,
        worker_id="w-dl",
    )
    worker = OutboxWorker(
        worker_id="w-dl",
        registry=HandlerRegistry(),
        claims=ActiveClaimRegistry(),
    )
    # retry_count 2 -> new_retry 3 >= max_retries 3 -> 进入 deadletter 路径
    worker._retry_or_deadletter(claimed, "injected handler crash")

    pg_session.expire_all()
    job = pg_session.get(OutboxJob, job_id)
    assert job is not None and job.status == "deadletter"
    assert job.terminal_reason, "deadletter job must carry a terminal_reason"
    assert job.claim_token is None and job.locked_by is None

    stage_run = pg_session.get(ForgetStageRun, "srun-dl")
    assert stage_run is not None and stage_run.status == "deadletter"
    assert stage_run.failure_count == 1

    operation = pg_session.get(ForgetOperation, op_id)
    assert operation is not None and operation.status == "shielded_deadletter"
    assert "deadletter" in (operation.error_message or "")


def test_advisory_lock_is_acquired_on_postgresql(pg_sessionmaker):
    """证明 _acquire_lock 在 PG 上真正持有 advisory 锁（非 SQLite 的 no-op）。

    若实现退化为 no-op，第二个会话会立即拿到锁，本用例失败。
    """
    from aiive.memory.memory_write_service import MemoryWriteService

    s_a = pg_sessionmaker()
    s_b = pg_sessionmaker()
    try:
        svc = MemoryWriteService(s_a)
        lock_id = svc._compute_lock_id("pg.contract.lock", "global", None)
        svc._acquire_lock(lock_id)

        s_b.execute(text("SET statement_timeout = '1s'"))
        with pytest.raises(OperationalError):
            s_b.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": lock_id})
    finally:
        s_a.rollback()
        s_a.close()
        s_b.rollback()
        s_b.close()
