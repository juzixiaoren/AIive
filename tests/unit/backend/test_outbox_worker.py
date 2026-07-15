"""Phase 0.5B OutboxWorker 基础单元测试。"""
import uuid

import pytest
from aiive.db.models import OutboxJob
from aiive.worker.outbox_worker import OutboxWorker
from aiive.worker.handler_registry import HandlerRegistry
from aiive.worker.outbox_heartbeat import ActiveClaimRegistry
from aiive.worker.outbox_dto import HandlerResult, HandlerOutcome, ClaimedJob


def _make_worker(allow=None):
    reg = HandlerRegistry()
    reg.register("test_job", lambda cj: HandlerResult(outcome=HandlerOutcome.COMPLETED), frozenset([1]))
    reg.register("core_memory_refresh", lambda cj: HandlerResult(outcome=HandlerOutcome.COMPLETED), frozenset([1]))
    claims = ActiveClaimRegistry()
    w = OutboxWorker(worker_id=f"test-{uuid.uuid4().hex[:8]}", registry=reg, claims=claims)
    if allow is not None:
        w._enabled_types = allow
    else:
        w._enabled_types = frozenset(["test_job", "core_memory_refresh"])
    return w


class TestOutboxWorker:
    def test_enqueue_creates_pending_job(self, db_session):
        w = _make_worker()
        job = w.enqueue(db_session, "test_job", {"key": "value"}, trace_id="t1")
        db_session.flush()
        assert job.status == "pending"
        assert job.payload == {"key": "value"}

    def test_enqueue_with_operation_id(self, db_session):
        w = _make_worker()
        job = w.enqueue(db_session, "test_job", {}, operation_id="op-1")
        db_session.flush()
        assert job.operation_id == "op-1"

    def test_multiple_enqueue(self, db_session):
        w = _make_worker()
        for i in range(3):
            w.enqueue(db_session, "test_job", {"i": i})
        db_session.commit()
        assert db_session.query(OutboxJob).count() == 3

    def test_claim_one(self, db_session, monkeypatch):
        monkeypatch.setattr("aiive.worker.outbox_worker.SessionLocal", lambda: db_session)
        w = _make_worker(allow=frozenset(["test_job"]))
        w.enqueue(db_session, "test_job", {"msg": "hello"})
        db_session.commit()
        claimed = w.claim_one()
        assert claimed is not None
        assert claimed.job_type == "test_job"

    def test_enqueue_rejects_disabled(self, db_session):
        w = _make_worker(allow=frozenset(["allowed_only"]))
        with pytest.raises(ValueError, match="enabled allowlist"):
            w.enqueue(db_session, "test_job", {})
