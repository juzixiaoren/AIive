"""Phase 0.5B: MemoryIngestionRun + Handler + Worker 集成测试。"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from aiive.db.models import (
    MemoryIngestionRun,
    MemoryProposal as MemoryProposalModel,
    OutboxJob,
    Thread,
    TurnRecord,
)
from aiive.worker.outbox_dto import (
    ClaimedJob,
    HandlerOutcome,
    HandlerResult,
)
from aiive.worker.handler_registry import HandlerRegistry
from aiive.worker.outbox_heartbeat import ActiveClaimRegistry
from aiive.worker.outbox_worker import OutboxWorker


# ============================================================================
# Helpers
# ============================================================================


def _make_turn_record(db_session, turn_id=None, thread_id=None, status="completed"):
    """Create a completed TurnRecord for testing."""
    if thread_id is None:
        thread = Thread(id=str(uuid.uuid4()), title="test-thread")
        db_session.add(thread)
        db_session.flush()
        thread_id = thread.id
    tr = TurnRecord(
        id=str(uuid.uuid4()),
        thread_id=thread_id,
        turn_id=turn_id or str(uuid.uuid4()),
        turn_sequence=1,
        status=status,
        request_fingerprint="test-fp",
    )
    db_session.add(tr)
    db_session.flush()
    return tr


def _make_claim(token, job_type="memory_extraction", payload=None, worker="test-worker"):
    return ClaimedJob(
        id=str(uuid.uuid4()),
        job_type=job_type,
        payload=payload or {},
        trace_id=str(uuid.uuid4()),
        retry_count=0,
        max_retries=3,
        claim_token=token,
        schema_version=1,
        worker_id=worker,
    )


# ============================================================================
# HandlerResult / Outcome 测试
# ============================================================================


class TestHandlerOutcome:
    def test_completed_does_not_retry(self, db_session, monkeypatch):
        """COMPLETED → Worker calls finalize_job."""
        monkeypatch.setattr("aiive.worker.outbox_worker.SessionLocal", lambda: db_session)

        reg = HandlerRegistry()
        reg.register("t", lambda cj: HandlerResult(HandlerOutcome.COMPLETED, "ok"), frozenset({1}))
        claims = ActiveClaimRegistry()
        w = OutboxWorker("w1", reg, claims)
        w._enabled_types = frozenset({"t"})

        w.enqueue(db_session, "t", {})
        db_session.commit()

        w.poll(max_jobs=1)
        db_session.flush()

        jobs = db_session.query(OutboxJob).all()
        assert len(jobs) == 1
        assert jobs[0].status == "completed"

    def test_non_retryable_deadletters(self, db_session, monkeypatch):
        """NON_RETRYABLE → Worker calls _deadletter_job_and_ingestion_run."""
        monkeypatch.setattr("aiive.worker.outbox_worker.SessionLocal", lambda: db_session)

        reg = HandlerRegistry()
        reg.register("t", lambda cj: HandlerResult(
            HandlerOutcome.NON_RETRYABLE, "bad-input",
            terminal_reason="test_reason",
        ), frozenset({1}))
        claims = ActiveClaimRegistry()
        w = OutboxWorker("w1", reg, claims)
        w._enabled_types = frozenset({"t"})

        w.enqueue(db_session, "t", {})
        db_session.commit()

        w.poll(max_jobs=1)
        db_session.flush()

        jobs = db_session.query(OutboxJob).all()
        assert len(jobs) == 1
        assert jobs[0].status == "deadletter"
        assert jobs[0].terminal_reason == "test_reason"

    def test_retry_later_resets_to_pending(self, db_session, monkeypatch):
        """RETRY_LATER → Worker calls _retry_later."""
        monkeypatch.setattr("aiive.worker.outbox_worker.SessionLocal", lambda: db_session)

        reg = HandlerRegistry()
        reg.register("t", lambda cj: HandlerResult(
            HandlerOutcome.RETRY_LATER, "busy",
            retry_available_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        ), frozenset({1}))
        claims = ActiveClaimRegistry()
        w = OutboxWorker("w1", reg, claims)
        w._enabled_types = frozenset({"t"})

        w.enqueue(db_session, "t", {})
        db_session.commit()

        w.poll(max_jobs=1)
        db_session.flush()

        jobs = db_session.query(OutboxJob).all()
        assert len(jobs) == 1
        assert jobs[0].status == "pending"
        assert jobs[0].available_at is not None

    def test_claim_lost_does_not_modify_outbox(self, db_session, monkeypatch):
        """CLAIM_LOST → Worker does not modify OutboxJob."""
        monkeypatch.setattr("aiive.worker.outbox_worker.SessionLocal", lambda: db_session)

        reg = HandlerRegistry()
        reg.register("t", lambda cj: HandlerResult(HandlerOutcome.CLAIM_LOST, "gone"), frozenset({1}))
        claims = ActiveClaimRegistry()
        w = OutboxWorker("w1", reg, claims)
        w._enabled_types = frozenset({"t"})

        w.enqueue(db_session, "t", {})
        db_session.commit()

        w.poll(max_jobs=1)
        db_session.flush()

        jobs = db_session.query(OutboxJob).all()
        assert len(jobs) == 1
        # Still running because CLAIM_LOST means "don't touch"
        assert jobs[0].status == "running"

    def test_unsupported_schema_rejected_by_worker(self, db_session, monkeypatch):
        """Worker rejects unsupported schema_version before calling handler."""
        monkeypatch.setattr("aiive.worker.outbox_worker.SessionLocal", lambda: db_session)

        handler_called = False

        def h(cj):
            nonlocal handler_called
            handler_called = True
            return HandlerResult(HandlerOutcome.COMPLETED, "ok")

        reg = HandlerRegistry()
        reg.register("t", h, frozenset({1}))  # only supports v1
        claims = ActiveClaimRegistry()
        w = OutboxWorker("w1", reg, claims)
        w._enabled_types = frozenset({"t"})

        # Manually create a job with schema_version=99
        db_session.add(OutboxJob(
            id=str(uuid.uuid4()),
            operation_id=f"op-{uuid.uuid4().hex[:8]}",
            job_type="t",
            status="pending",
            payload={},
            max_retries=3,
            schema_version=99,
        ))
        db_session.commit()

        w.poll(max_jobs=1)
        db_session.flush()

        assert not handler_called
        jobs = db_session.query(OutboxJob).all()
        assert jobs[0].status == "deadletter"
        assert jobs[0].terminal_reason == "unsupported_schema_version"

    def test_retryable_error_retries(self, db_session, monkeypatch):
        """RETRYABLE_ERROR → Worker retry_or_deadletter."""
        monkeypatch.setattr("aiive.worker.outbox_worker.SessionLocal", lambda: db_session)

        reg = HandlerRegistry()
        reg.register("t", lambda cj: HandlerResult(
            HandlerOutcome.RETRYABLE_ERROR, "oops",
            ingestion_run_id="ir-1",
        ), frozenset({1}))
        claims = ActiveClaimRegistry()
        w = OutboxWorker("w1", reg, claims)
        w._enabled_types = frozenset({"t"})

        w.enqueue(db_session, "t", {})
        db_session.commit()

        w.poll(max_jobs=1)
        db_session.flush()

        jobs = db_session.query(OutboxJob).all()
        assert jobs[0].status == "pending"
        assert jobs[0].retry_count == 1


# ============================================================================
# IngestionRun 测试
# ============================================================================


class TestIngestionRunResolution:
    def test_on_conflict_create_single_run(self, db_session):
        """INSERT ON CONFLICT DO NOTHING: 并发只创建一条 Run。"""
        turn = _make_turn_record(db_session)

        now = datetime.now(timezone.utc)
        values = {
            "id": str(uuid.uuid4()),
            "source_turn_record_id": turn.id,
            "extractor_name": "test",
            "extractor_version": "1.0",
            "status": "pending",
            "proposal_count": 0,
            "created_at": now,
        }
        # First insert
        db_session.execute(text("""
            INSERT INTO memory_ingestion_runs
                (id, source_turn_record_id, extractor_name, extractor_version, status, proposal_count, created_at)
            VALUES (:id, :source_turn_record_id, :extractor_name, :extractor_version, :status, :proposal_count, :created_at)
            ON CONFLICT (source_turn_record_id, extractor_name, extractor_version) DO NOTHING
        """), values)
        db_session.flush()

        # Second insert (same key)
        db_session.execute(text("""
            INSERT INTO memory_ingestion_runs
                (id, source_turn_record_id, extractor_name, extractor_version, status, proposal_count, created_at)
            VALUES (:id, :source_turn_record_id, :extractor_name, :extractor_version, :status, :proposal_count, :created_at)
            ON CONFLICT (source_turn_record_id, extractor_name, extractor_version) DO NOTHING
        """), {**values, "id": str(uuid.uuid4())})
        db_session.commit()

        runs = db_session.query(MemoryIngestionRun).all()
        assert len(runs) == 1
        assert runs[0].source_turn_record_id == turn.id


# ============================================================================
# ActiveClaimRegistry 测试
# ============================================================================


class TestActiveClaimRegistry:
    def test_same_job_different_token_no_overwrite(self):
        reg = ActiveClaimRegistry()
        from aiive.worker.outbox_dto import ActiveClaim
        c1 = ActiveClaim("j1", "t1", "w1", datetime.now(timezone.utc))
        c2 = ActiveClaim("j1", "t2", "w2", datetime.now(timezone.utc))
        reg.add(c1)
        reg.add(c2)
        # Adding c2 should mark c1 lost
        assert c1.lost is True
        assert c2.lost is False
        # Only c2 should be in snapshot
        snap = reg.get_snapshot()
        assert len(snap) == 1
        assert snap[0].claim_token == "t2"

    def test_remove_only_specified_claim(self):
        reg = ActiveClaimRegistry()
        from aiive.worker.outbox_dto import ActiveClaim
        c1 = ActiveClaim("j1", "t1", "w1", datetime.now(timezone.utc))
        reg.add(c1)
        reg.remove("j1", "t1")
        assert reg.is_lost("j1", "t1") is True


# ============================================================================
# WriteOutcome 测试
# ============================================================================


class TestWriteOutcome:
    def test_gate_rejected_is_valid_noop(self, db_session):
        """GATE_REJECTED 属于合法 no-op，不导致批次失败。"""
        from aiive.memory.memory_types import WriteOutcome
        from aiive.memory.memory_write_service import MemoryWriteService
        w = MemoryWriteService(db_session)
        assert WriteOutcome.GATE_REJECTED in w._VALID_NOOP_OUTCOMES
        assert WriteOutcome.IGNORED in w._VALID_NOOP_OUTCOMES
        assert WriteOutcome.REINFORCE_SKIPPED in w._VALID_NOOP_OUTCOMES


# ============================================================================
# Worker infrastructure
# ============================================================================


class TestWorkerLifecycle:
    def test_claim_one_marks_running_with_token(self, db_session, monkeypatch):
        monkeypatch.setattr("aiive.worker.outbox_worker.SessionLocal", lambda: db_session)
        reg = HandlerRegistry()
        reg.register("t", lambda cj: HandlerResult(HandlerOutcome.COMPLETED, "ok"), frozenset({1}))
        claims = ActiveClaimRegistry()
        w = OutboxWorker("w1", reg, claims)
        w._enabled_types = frozenset({"t"})

        w.enqueue(db_session, "t", {})
        db_session.commit()

        claimed = w.claim_one()
        assert claimed is not None
        assert claimed.claim_token is not None
        assert claimed.worker_id == "w1"

        db_session.flush()
        job = db_session.get(OutboxJob, claimed.id)
        assert job.status == "running"
        assert job.claim_token == claimed.claim_token
        assert job.locked_by == "w1"
        assert job.lease_expires_at is not None

    def test_multiple_claim_one_no_duplicates(self, db_session, monkeypatch):
        monkeypatch.setattr("aiive.worker.outbox_worker.SessionLocal", lambda: db_session)
        reg = HandlerRegistry()
        reg.register("t", lambda cj: HandlerResult(HandlerOutcome.COMPLETED, "ok"), frozenset({1}))
        claims = ActiveClaimRegistry()
        w = OutboxWorker("w1", reg, claims)
        w._enabled_types = frozenset({"t"})

        for _ in range(3):
            w.enqueue(db_session, "t", {})
        db_session.commit()

        claimed = w.claim_one()
        assert claimed is not None

        # Second claim should not return the same job (status==running with valid lease)
        claimed2 = w.claim_one()
        if claimed2 is not None:
            assert claimed2.id != claimed.id

    def test_deadletter_clears_token_and_lease(self, db_session, monkeypatch):
        monkeypatch.setattr("aiive.worker.outbox_worker.SessionLocal", lambda: db_session)
        reg = HandlerRegistry()
        reg.register("t", lambda cj: HandlerResult(
            HandlerOutcome.NON_RETRYABLE, "bad", terminal_reason="test_reason",
        ), frozenset({1}))
        claims = ActiveClaimRegistry()
        w = OutboxWorker("w1", reg, claims)
        w._enabled_types = frozenset({"t"})

        w.enqueue(db_session, "t", {})
        db_session.commit()
        w.poll(max_jobs=1)
        db_session.flush()

        job = db_session.query(OutboxJob).first()
        assert job.status == "deadletter"
        assert job.claim_token is None
        assert job.locked_by is None
        assert job.lease_expires_at is None
        assert job.terminal_reason == "test_reason"


# ============================================================================
# MemoryProposal source_turn_id 传播
# ============================================================================


class TestSourceTurnId:
    def test_source_turn_id_persisted(self, db_session):
        """source_turn_id 写入 DB 后正确持久化。"""
        turn = _make_turn_record(db_session)
        mp = MemoryProposalModel(
            id=str(uuid.uuid4()),
            proposal_id=str(uuid.uuid4()),
            source_turn_id=turn.turn_id,
            ingestion_run_id=str(uuid.uuid4()),
            proposal_index=0,
            memory_type="knowledge",
            canonical_key="test.key",
            content="test",
        )
        db_session.add(mp)
        db_session.commit()

        saved = db_session.get(MemoryProposalModel, mp.id)
        assert saved.source_turn_id == turn.turn_id
        assert saved.source_turn_id != ""
        assert saved.ingestion_run_id is not None
        assert saved.proposal_index == 0
