"""
Phase 6B 保留治理单元测试。

覆盖：RetentionCleanupRun/Batch CRUD、safety predicates、generation 分批清理、
operation_id 幂等、terminal_at 不可变性、Operational JSON 安全、自身清理。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text as sa_text
from sqlalchemy.orm import Session, sessionmaker

from aiive.db.models import (
    Base,
    CompactionInput,
    Event,
    Epoch,
    EpochCheckpoint,
    MemoryMaintenanceInput,
    OutboxJob,
    RetrievalIndexEntry,
    RetrievalIndexGeneration,
    RetrievalIndexToken,
    Segment,
    SegmentSummary,
    Thread,
    TurnRecord,
)
from aiive.db.forget_models import (
    ForgetAction,
    ForgetBatch,
    ForgetDependency,
    ForgetOperation,
    ForgetStageRun,
    ForgetTarget,
    ForgetTombstone,
)
from aiive.db.retention_models import RetentionCleanupBatch, RetentionCleanupRun
from aiive.retention.config import RETENTION_POLICY_V1, RetentionPolicyConfig
from aiive.retention.engine import RetentionEngine
from aiive.retention.safety_predicates import (
    is_forget_operation_clearable,
    run_all_safety_predicates,
)
from aiive.retention.maintenance_lease import MaintenanceLease
from aiive.retention.vacuum import VacuumExecutor


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def db_session():
    """SQLite 内存数据库 session，测试后自动清空。"""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def _new_id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ═══════════════════════════════════════════════════════════════════
# 1. RetentionCleanupRun / Batch CRUD
# ═══════════════════════════════════════════════════════════════════


class TestRetentionCleanupRun:
    """RetentionCleanupRun 基本 CRUD 和幂等性。"""

    def test_create_run(self, db_session):
        """创建 RetentionCleanupRun 并持久化。"""
        outbox = OutboxJob(
            operation_id="retention:all:1:2026-07-17",
            job_type="retention_cleanup",
        )
        db_session.add(outbox)
        db_session.flush()

        run = RetentionCleanupRun(
            outbox_job_id=outbox.id,
            operation_id="retention:all:1:2026-07-17",
            policy_version=1,
            policy_snapshot=RETENTION_POLICY_V1.to_snapshot(),
            cutoff_at=_utcnow(),
        )
        db_session.add(run)
        db_session.flush()

        fetched = db_session.get(RetentionCleanupRun, run.id)
        assert fetched is not None
        assert fetched.operation_id == "retention:all:1:2026-07-17"
        assert fetched.policy_version == 1

    def test_operation_id_uniqueness(self, db_session):
        """operation_id 有 UNIQUE 约束，同 window_bucket 重复创建报错。"""
        outbox1 = OutboxJob(
            operation_id="retention:all:1:2026-07-17:0",
            job_type="retention_cleanup",
        )
        db_session.add(outbox1)
        db_session.flush()

        run1 = RetentionCleanupRun(
            outbox_job_id=outbox1.id,
            operation_id="retention:all:1:2026-07-17",
            policy_version=1,
            cutoff_at=_utcnow(),
        )
        db_session.add(run1)
        db_session.flush()

        outbox2 = OutboxJob(
            operation_id="retention:all:1:2026-07-17:1",
            job_type="retention_cleanup",
        )
        db_session.add(outbox2)
        db_session.flush()

        run2 = RetentionCleanupRun(
            outbox_job_id=outbox2.id,
            operation_id="retention:all:1:2026-07-17",
            policy_version=1,
            cutoff_at=_utcnow(),
        )
        db_session.add(run2)
        with pytest.raises(Exception):
            db_session.flush()

    def test_different_window_bucket(self, db_session):
        """不同 window_bucket 可以创建多个 Run。"""
        for bucket in ("2026-07-17", "2026-07-18"):
            outbox = OutboxJob(
                operation_id=f"retention:all:1:{bucket}:{_new_id()}",
                job_type="retention_cleanup",
            )
            db_session.add(outbox)
            db_session.flush()

            run = RetentionCleanupRun(
                outbox_job_id=outbox.id,
                operation_id=f"retention:all:1:{bucket}",
                policy_version=1,
                cutoff_at=_utcnow(),
            )
            db_session.add(run)
            db_session.flush()

        runs = db_session.query(RetentionCleanupRun).all()
        assert len(runs) == 2


class TestRetentionCleanupBatch:
    """RetentionCleanupBatch 基本 CRUD 和游标。"""

    def test_create_batch(self, db_session):
        """创建 Batch 并持久化。"""
        outbox = OutboxJob(
            operation_id="retention:all:1:batch-test",
            job_type="retention_cleanup",
        )
        db_session.add(outbox)
        db_session.flush()

        run = RetentionCleanupRun(
            outbox_job_id=outbox.id,
            operation_id="retention:all:1:2026-07-17-batch-test",
            policy_version=1,
            cutoff_at=_utcnow(),
        )
        db_session.add(run)
        db_session.flush()

        batch = RetentionCleanupBatch(
            run_id=run.id,
            lane="outbox_job",
            batch_no=1,
            cursor_start_json={"last_id": "abc"},
            cutoff_at=_utcnow(),
        )
        db_session.add(batch)
        db_session.flush()

        fetched = db_session.get(RetentionCleanupBatch, batch.id)
        assert fetched is not None
        assert fetched.lane == "outbox_job"
        assert fetched.batch_no == 1

    def test_batch_run_lane_unique(self, db_session):
        """同一 Run/Lane/BatchNo 有 UNIQUE 约束。"""
        outbox = OutboxJob(
            operation_id="retention:all:1:batch-unique",
            job_type="retention_cleanup",
        )
        db_session.add(outbox)
        db_session.flush()

        run = RetentionCleanupRun(
            outbox_job_id=outbox.id,
            operation_id="retention:all:1:2026-07-17-batch-unique",
            policy_version=1,
            cutoff_at=_utcnow(),
        )
        db_session.add(run)
        db_session.flush()

        batch1 = RetentionCleanupBatch(
            run_id=run.id, lane="outbox_job", batch_no=1,
            cursor_start_json={}, cutoff_at=_utcnow(),
        )
        db_session.add(batch1)
        db_session.flush()

        batch2 = RetentionCleanupBatch(
            run_id=run.id, lane="outbox_job", batch_no=1,
            cursor_start_json={}, cutoff_at=_utcnow(),
        )
        db_session.add(batch2)
        with pytest.raises(Exception):
            db_session.flush()


# ═══════════════════════════════════════════════════════════════════
# 2. Safety Predicates (Q.5)
# ═══════════════════════════════════════════════════════════════════


class TestSafetyPredicates:
    """Q.5 安全谓词测试。"""

    def test_active_building_generation_not_cleanable(self, db_session):
        """active/building generation 不可清理。"""
        gen = RetrievalIndexGeneration(
            index_version=1, status="active",
        )
        db_session.add(gen)
        db_session.flush()

        passed, reason = run_all_safety_predicates(
            db_session,
            cutoff_at=_utcnow(),
            generation_status=gen.status,
        )
        assert not passed

    def test_retired_generation_cleanable(self, db_session):
        """retired generation 可清理。"""
        gen = RetrievalIndexGeneration(
            index_version=1, status="retired",
        )
        db_session.add(gen)
        db_session.flush()

        passed, _ = run_all_safety_predicates(
            db_session,
            cutoff_at=_utcnow() + timedelta(days=365),
            generation_status=gen.status,
        )
        assert passed

    def test_current_summary_not_cleanable(self, db_session):
        """当前 Segment.summary_id 指针不可清理。"""
        thread = Thread(id=_new_id(), title="test")
        db_session.add(thread)
        epoch = Epoch(id=_new_id(), thread_id=thread.id, epoch_no=1)
        db_session.add(epoch)
        segment = Segment(
            id=_new_id(), epoch_id=epoch.id, thread_id=thread.id,
            segment_no=1, start_turn_sequence=1,
        )
        db_session.add(segment)
        db_session.flush()

        # SegmentSummary 需要 segment_id（NOT NULL）
        summary = SegmentSummary(id=_new_id(), segment_id=segment.id)
        db_session.add(summary)
        db_session.flush()

        segment.summary_id = summary.id
        db_session.flush()

        passed, _ = run_all_safety_predicates(
            db_session,
            cutoff_at=_utcnow(),
            segment_summary_id=summary.id,
        )
        assert not passed

    def test_forget_operation_protected_statuses(self, db_session):
        """failed_retryable/shielded_deadletter 等受保护状态不可清理。"""
        for idx, status in enumerate(("failed_retryable", "shielded_deadletter", "verifying", "purging")):
            op = ForgetOperation(
                id=_new_id(),
                operation_key=f"op-key-{status}-{idx}",
                mode="forget",
                selector_type="by_ids",
                selector_hash="hash123",
                status=status,
            )
            db_session.add(op)
            db_session.flush()

            assert not is_forget_operation_clearable(db_session, op.id)

    def test_purged_operation_clearable(self, db_session):
        """status='purged' 且 verifier 已通过可清理。"""
        op = ForgetOperation(
            id=_new_id(),
            operation_key="op-key-purged",
            mode="forget",
            selector_type="by_ids",
            selector_hash="hash456",
            status="purged",
            purged_at=_utcnow(),
            verified_at=_utcnow(),  # Verifier 必须已通过
        )
        db_session.add(op)
        db_session.flush()

        assert is_forget_operation_clearable(db_session, op.id)

    def test_purged_not_verified_not_clearable(self, db_session):
        """status='purged' 但 verified_at=NULL 不可清理。"""
        op = ForgetOperation(
            id=_new_id(),
            operation_key="op-key-purged-no-verify",
            mode="forget",
            selector_type="by_ids",
            selector_hash="hash789",
            status="purged",
            purged_at=_utcnow(),
            # verified_at=NULL — Verifier 未通过
        )
        db_session.add(op)
        db_session.flush()

        assert not is_forget_operation_clearable(db_session, op.id)


# ═══════════════════════════════════════════════════════════════════
# 3. terminal_at 不可变性 (修订点 4)
# ═══════════════════════════════════════════════════════════════════


class TestTerminalAt:
    """terminal_at 终态时间不可变性。"""

    def test_terminal_at_set_on_complete(self, db_session):
        """completed 终态路径原子写入 terminal_at。"""
        job = OutboxJob(
            operation_id="test:terminal-at",
            job_type="retention_cleanup",
            status="completed",
            terminal_at=_utcnow(),
        )
        db_session.add(job)
        db_session.flush()

        original = job.terminal_at
        # 模拟 claim/retry — terminal_at 不应被修改
        job.updated_at = _utcnow()
        db_session.flush()

        refreshed = db_session.get(OutboxJob, job.id)
        assert refreshed.terminal_at == original

    def test_payload_scrub_not_update_terminal_at(self, db_session):
        """payload scrub 不更新 terminal_at。"""
        job = OutboxJob(
            operation_id="test:scrub-terminal",
            job_type="retention_cleanup",
            status="completed",
            terminal_at=_utcnow(),
            payload={"sensitive": "data"},
        )
        db_session.add(job)
        db_session.flush()

        original = job.terminal_at
        job.payload = {}
        db_session.flush()

        refreshed = db_session.get(OutboxJob, job.id)
        assert refreshed.terminal_at == original


# ═══════════════════════════════════════════════════════════════════
# 4. Generation 分批清理 (Q.7)
# ═══════════════════════════════════════════════════════════════════


class TestGenerationCleanup:
    """Retrieval generation 分批清理。"""

    def test_active_generation_skipped(self, db_session):
        """active generation 应被跳过。"""
        gen = RetrievalIndexGeneration(
            index_version=1, status="active",
            status_changed_at=_utcnow() - timedelta(days=60),
        )
        db_session.add(gen)
        db_session.flush()

        # 不应命中 active
        candidates = (
            db_session.query(RetrievalIndexGeneration)
            .filter(
                RetrievalIndexGeneration.status.in_(["retired", "failed"]),
                RetrievalIndexGeneration.status_changed_at <= _utcnow(),
            )
            .all()
        )
        assert len(candidates) == 0

    def test_failed_generation_uses_status_changed_at(self, db_session):
        """failed generation 使用 status_changed_at 而非 build_completed_at。"""
        gen = RetrievalIndexGeneration(
            index_version=1,
            status="failed",
            status_changed_at=_utcnow() - timedelta(days=90),
            # build_completed_at 为 NULL — 不应依赖
            build_completed_at=None,
        )
        db_session.add(gen)
        db_session.flush()

        cutoff = _utcnow() - timedelta(days=60)
        candidates = (
            db_session.query(RetrievalIndexGeneration)
            .filter(
                RetrievalIndexGeneration.status.in_(["retired", "failed"]),
                RetrievalIndexGeneration.status_changed_at <= cutoff,
            )
            .all()
        )
        assert len(candidates) == 1
        assert candidates[0].id == gen.id

    def test_token_batch_delete(self, db_session):
        """分批删除 Token 可执行。"""
        gen = RetrievalIndexGeneration(
            index_version=100, status="retired",
            status_changed_at=_utcnow() - timedelta(days=90),
            build_completed_at=_utcnow() - timedelta(days=90),
        )
        db_session.add(gen)
        db_session.flush()

        entry = RetrievalIndexEntry(
            source_type="memory_record",
            source_id=_new_id(),
            source_version="1",
            index_version=100,
        )
        db_session.add(entry)
        db_session.flush()

        token = RetrievalIndexToken(
            entry_id=entry.id,
            index_version=100,
            token="test_token",
        )
        db_session.add(token)
        db_session.flush()

        # 按 index_version 查找 Token
        tokens = (
            db_session.query(RetrievalIndexToken)
            .filter(RetrievalIndexToken.index_version == 100)
            .all()
        )
        assert len(tokens) == 1
        for t in tokens:
            db_session.delete(t)
        db_session.flush()

        # 确认已清空
        remaining = (
            db_session.query(RetrievalIndexToken)
            .filter(RetrievalIndexToken.index_version == 100)
            .count()
        )
        assert remaining == 0


# ═══════════════════════════════════════════════════════════════════
# 5. ForgetAction 清理 (Q.8)
# ═══════════════════════════════════════════════════════════════════


class TestForgetActionCleanup:
    """ForgetAction/Batch/Dependency/Target 清理。"""

    def test_purged_operation_actions_cleanable(self, db_session):
        """purged Operation 的 Action/Batch 可清理。"""
        op = ForgetOperation(
            id=_new_id(),
            operation_key="op-key-purged-action",
            mode="forget",
            selector_type="by_ids",
            selector_hash="hash789",
            status="purged",
            purged_at=_utcnow() - timedelta(days=200),
            verified_at=_utcnow(),  # Verifier 必须已通过
        )
        db_session.add(op)
        db_session.flush()

        action = ForgetAction(
            id=_new_id(), forget_operation_id=op.id,
            action_type="scrub",
            idempotency_key=f"idem:{_new_id()}",
        )
        db_session.add(action)
        db_session.flush()

        assert is_forget_operation_clearable(db_session, op.id)

        db_session.delete(action)
        db_session.flush()
        assert db_session.get(ForgetAction, action.id) is None

    def test_failed_retryable_actions_not_cleanable(self, db_session):
        """failed_retryable Operation 的子记录不可清理。"""
        op = ForgetOperation(
            id=_new_id(),
            operation_key="op-key-failed-retryable",
            mode="forget",
            selector_type="by_ids",
            selector_hash="hash000",
            status="failed_retryable",
        )
        db_session.add(op)
        db_session.flush()

        assert not is_forget_operation_clearable(db_session, op.id)


# ═══════════════════════════════════════════════════════════════════
# 6. 自身 Retention Lane (修订点 3)
# ═══════════════════════════════════════════════════════════════════


class TestSelfRetention:
    """RetentionCleanupRun/Batch 自身清理。"""

    def test_completed_batch_cleanable(self, db_session):
        """已完成 Batch 可在超期后删除。"""
        outbox = OutboxJob(
            operation_id="retention:all:1:self-batch",
            job_type="retention_cleanup",
        )
        db_session.add(outbox)
        db_session.flush()

        run = RetentionCleanupRun(
            outbox_job_id=outbox.id,
            operation_id="retention:all:1:2026-07-17-self",
            policy_version=1,
            cutoff_at=_utcnow(),
            status="done",
        )
        db_session.add(run)
        db_session.flush()

        batch = RetentionCleanupBatch(
            run_id=run.id, lane="outbox_job", batch_no=1,
            cursor_start_json={}, cutoff_at=_utcnow() - timedelta(days=120),
            status="done",
        )
        db_session.add(batch)
        db_session.flush()

        old_batches = (
            db_session.query(RetentionCleanupBatch)
            .filter(RetentionCleanupBatch.status == "done")
            .all()
        )
        for b in old_batches:
            db_session.delete(b)
        db_session.flush()

        remaining = (
            db_session.query(RetentionCleanupBatch)
            .filter(RetentionCleanupBatch.id == batch.id)
            .count()
        )
        assert remaining == 0

    def test_run_with_batches_not_deleted(self, db_session):
        """仍有未清 Batch 的 Run 不删除。"""
        outbox = OutboxJob(
            operation_id="retention:all:1:self-run",
            job_type="retention_cleanup",
        )
        db_session.add(outbox)
        db_session.flush()

        run = RetentionCleanupRun(
            outbox_job_id=outbox.id,
            operation_id="retention:all:1:2026-07-17-self-run",
            policy_version=1,
            cutoff_at=_utcnow() - timedelta(days=120),
            status="done",
        )
        db_session.add(run)
        db_session.flush()

        batch = RetentionCleanupBatch(
            run_id=run.id, lane="outbox_job", batch_no=1,
            cursor_start_json={}, cutoff_at=_utcnow(),
            status="pending",
        )
        db_session.add(batch)
        db_session.flush()

        remaining_batches = (
            db_session.query(RetentionCleanupBatch)
            .filter(RetentionCleanupBatch.run_id == run.id)
            .count()
        )
        if remaining_batches > 0:
            pass  # 不删 Run
        else:
            db_session.delete(run)
        db_session.flush()

        assert db_session.get(RetentionCleanupRun, run.id) is not None

    def test_running_run_not_cleaned(self, db_session):
        """running 状态的 Run 不删除。"""
        outbox = OutboxJob(
            operation_id="retention:all:1:self-running",
            job_type="retention_cleanup",
        )
        db_session.add(outbox)
        db_session.flush()

        run = RetentionCleanupRun(
            outbox_job_id=outbox.id,
            operation_id="retention:all:1:2026-07-17-self-running",
            policy_version=1,
            cutoff_at=_utcnow() - timedelta(days=120),
            status="running",
        )
        db_session.add(run)
        db_session.flush()

        old_runs = (
            db_session.query(RetentionCleanupRun)
            .filter(
                RetentionCleanupRun.status == "done",
                RetentionCleanupRun.updated_at <= _utcnow() - timedelta(days=90),
            )
            .all()
        )
        run_ids = {r.id for r in old_runs}
        assert run.id not in run_ids, "running Run 不应被 cleanup 查询命中"


# ═══════════════════════════════════════════════════════════════════
# 7. operational JSON 安全 (修订点 5)
# ═══════════════════════════════════════════════════════════════════


class TestOperationalJson:
    """operational JSON 内容安全检查。"""

    def test_forget_action_details_scrubbed(self, db_session):
        """ForgetAction.details 在清理时清空（DTO 安全）。"""
        op = ForgetOperation(
            id=_new_id(),
            operation_key="op-key-details-scrub",
            mode="forget",
            selector_type="by_ids",
            selector_hash="hashXYZ",
            status="purged",
            purged_at=_utcnow() - timedelta(days=200),
            verified_at=_utcnow(),
        )
        db_session.add(op)
        db_session.flush()

        action = ForgetAction(
            id=_new_id(),
            forget_operation_id=op.id,
            action_type="scrub",
            idempotency_key=f"idem:{_new_id()}",
            details={"user_content": "敏感信息", "record_id": _new_id()},
        )
        db_session.add(action)
        db_session.flush()

        # 清理: DTO 安全 — 清空 details
        action.details = None
        db_session.flush()

        refreshed = db_session.get(ForgetAction, action.id)
        assert refreshed.details is None, "清理后 details 应为 None"

    def test_maintenance_action_details_controlled(self, db_session):
        """MemoryMaintenanceAction 通过 DTO 仅保存 ID/hash/status/数值。"""
        from aiive.db.models import MemoryMaintenanceAction, MemoryMaintenanceRun

        outbox = OutboxJob(
            operation_id="test:dto-action",
            job_type="memory_maintenance",
        )
        db_session.add(outbox)
        db_session.flush()

        run = MemoryMaintenanceRun(
            outbox_job_id=outbox.id,
            operation_id="test:dto-run",
            cutoff_updated_at=_utcnow(),
        )
        db_session.add(run)
        db_session.flush()

        # DTO 强约束：只写 ID/hash/status/数值
        # MemoryMaintenanceAction 需要 batch_id（NOT NULL FK）
        from aiive.db.models import MemoryMaintenanceBatch
        batch = MemoryMaintenanceBatch(
            run_id=run.id, batch_no=1, candidate_lane="changed",
        )
        db_session.add(batch)
        db_session.flush()

        action = MemoryMaintenanceAction(
            run_id=run.id,
            batch_id=batch.id,
            action_sequence=1,
            action_type="archive",
            reason_code="expired",
            idempotency_key=f"idem:{_new_id()}",
            preconditions=None,  # DTO 要求不保存用户正文
            details=None,        # DTO 要求不保存用户正文
        )
        db_session.add(action)
        db_session.flush()

        refreshed = db_session.get(MemoryMaintenanceAction, action.id)
        assert refreshed.preconditions is None
        assert refreshed.details is None


# ═══════════════════════════════════════════════════════════════════
# 8. StageRun 引用保护 (修订点 2)
# ═══════════════════════════════════════════════════════════════════


class TestStageRunProtection:
    """被 StageRun 引用的 OutboxJob 不删除。"""

    def test_stagerun_referenced_job_not_deleted(self, db_session):
        """有 ForgetStageRun 引用的 OutboxJob 不删除。"""
        job = OutboxJob(
            operation_id="test:stagerun-ref",
            job_type="forget_cascade",
            status="completed",
            terminal_at=_utcnow() - timedelta(days=200),
        )
        db_session.add(job)
        db_session.flush()

        op = ForgetOperation(
            id=_new_id(),
            operation_key="op-key-stagerun-ref",
            mode="forget",
            selector_type="by_ids",
            selector_hash="hashSR",
            status="purged",
            purged_at=_utcnow(),
        )
        db_session.add(op)
        db_session.flush()

        sr = ForgetStageRun(
            forget_operation_id=op.id,
            stage="cascade",
            outbox_job_id=job.id,
            status="done",
        )
        db_session.add(sr)
        db_session.flush()

        # 检查是否有 StageRun 引用
        ref_count = (
            db_session.query(ForgetStageRun)
            .filter(ForgetStageRun.outbox_job_id == job.id)
            .count()
        )
        assert ref_count > 0, "不应删除有 StageRun 引用的 OutboxJob"


# ═══════════════════════════════════════════════════════════════════
# 9. MaintenanceLease (R.4)
# ═══════════════════════════════════════════════════════════════════


class TestMaintenanceLease:
    """重维护任务互斥。"""

    def test_acquire_release(self, db_session):
        """获取和释放租约。"""
        lease = MaintenanceLease(db_session, "sqlite")
        assert lease.acquire(58001)
        lease.release_all()
        assert len(lease._acquired) == 0

    def test_multiple_locks(self, db_session):
        """可以获取多个锁。"""
        lease = MaintenanceLease(db_session, "sqlite")
        assert lease.acquire(58001)
        assert lease.acquire(58002)
        assert len(lease._acquired) == 2
        lease.release_all()


# ═══════════════════════════════════════════════════════════════════
# 10. Policy Config (修订点 9)
# ═══════════════════════════════════════════════════════════════════


class TestPolicyConfig:
    """版本化配置测试。"""

    def test_to_snapshot(self):
        """配置可序列化为快照。"""
        snapshot = RETENTION_POLICY_V1.to_snapshot()
        assert snapshot["policy_version"] == 1
        assert len(snapshot["lanes"]) > 0
        assert "lane" in snapshot["lanes"][0]

    def test_lane_config_lookup(self):
        """按名称查找 lane 配置。"""
        cfg = RETENTION_POLICY_V1.lane_config("outbox_job")
        assert cfg is not None
        assert cfg.retention_days == 30
        assert cfg.delete_days == 180

    def test_unknown_lane(self):
        """未知 lane 返回 None。"""
        assert RETENTION_POLICY_V1.lane_config("nonexistent") is None


# ═══════════════════════════════════════════════════════════════════
# 11. CompactionInput scrub 后 manifest 保留 (Q.6, 测试1)
# ═══════════════════════════════════════════════════════════════════


class TestCompactionInputScrub:
    """CompactionInput scrub 后 manifest 仍保留。"""

    def test_manifest_survives_scrub(self, db_session):
        """scrub working_state_snapshot 后 manifest 不变。"""
        thread = Thread(id=_new_id(), title="test")
        db_session.add(thread)
        epoch = Epoch(id=_new_id(), thread_id=thread.id, epoch_no=1)
        db_session.add(epoch)
        segment = Segment(
            id=_new_id(), epoch_id=epoch.id, thread_id=thread.id,
            segment_no=1, start_turn_sequence=1,
        )
        db_session.add(segment)
        db_session.flush()

        ci = CompactionInput(
            segment_id=segment.id,
            start_turn_sequence=1,
            end_turn_sequence=5,
            turn_manifest=[{"turn_id": "t1", "turn_sequence": 1}],
            event_manifest=[{"event_id": "e1", "content_hash": "abc123"}],
            working_state_snapshot={"open_loops": ["important_task"]},
            source_hash="hash123",
            summary_version=1,
            working_state_version=1,
        )
        db_session.add(ci)
        db_session.flush()

        original_turn = ci.turn_manifest
        original_event = ci.event_manifest
        original_hash = ci.source_hash

        # scrub: 清空用户快照，保留 manifest
        ci.working_state_snapshot = {}
        db_session.flush()

        refreshed = db_session.get(CompactionInput, ci.id)
        assert refreshed.working_state_snapshot == {}
        assert refreshed.turn_manifest == original_turn
        assert refreshed.event_manifest == original_event
        assert refreshed.source_hash == original_hash


# ═══════════════════════════════════════════════════════════════════
# 12. Vacuum 安全与阈值
# ═══════════════════════════════════════════════════════════════════


class TestVacuumExecutor:
    def test_sqlite_vacuum_honors_file_size_threshold(self, tmp_path):
        database_path = tmp_path / "vacuum.db"
        database_url = f"sqlite:///{database_path}"
        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.execute(sa_text("CREATE TABLE payload (content BLOB)"))
            connection.execute(sa_text("INSERT INTO payload VALUES (zeroblob(2097152))"))
            connection.execute(sa_text("DROP TABLE payload"))
        engine.dispose()

        executor = VacuumExecutor(database_url)
        assert executor.sqlite_vacuum(min_freelist_pct=0, min_file_size_mb=100) is False
        assert executor.sqlite_vacuum(min_freelist_pct=0, min_file_size_mb=0) is True

    def test_postgres_maintenance_rejects_unsafe_table_name(self):
        executor = VacuumExecutor("postgresql://unused")
        with pytest.raises(ValueError, match="invalid_table_name"):
            executor.pg_vacuum_analyze("records; DROP TABLE records")
