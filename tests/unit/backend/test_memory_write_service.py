"""Test MemoryWriteService: transactional write, reinforce, supersede, forget."""
import itertools

from aiive.context.run_context import RunContext
from aiive.memory.memory_store import MemoryStore
from aiive.memory.memory_types import (
    MemoryProposal,
    EvidenceItem,
    LifecycleState,
    TrustLevel,
)
from aiive.memory.memory_write_service import MemoryWriteService
from aiive.db.models import MemoryEvidence, MemoryLineage, MemoryProposal as MemoryProposalModel

_proposal_counter = itertools.count(1)


def _make_ctx(thread_id: str = "thread-1", trace_id: str = "trace-1") -> RunContext:
    return RunContext(thread_id=thread_id, trace_id=trace_id, source="test")


def _make_proposal(key: str, content: str, mem_type: str = "user_profile") -> MemoryProposal:
    idx = next(_proposal_counter)
    p = MemoryProposal(
        memory_type=mem_type, canonical_key=key, content=content, confidence=0.95,
        source_event_ids=[f"evt-{idx}"],
        evidence=[EvidenceItem(source_type="user_message", trust_level=TrustLevel.TRUSTED.value)],
        extractor_name="test", extractor_version="1.0",
    )
    p.compute_request_idempotency()
    return p


class TestMemoryWriteService:
    def test_create_active_record(self, db_session):
        writer = MemoryWriteService(db_session)
        proposal = _make_proposal("user.preference.coffee", "Loves coffee")
        result = writer.write(proposal, run_context=_make_ctx())

        assert result.written
        assert result.operation == "create"
        assert result.state == LifecycleState.ACTIVE.value

        # Verify DB
        store = MemoryStore(db_session)
        record = store.get_by_id(result.memory_id)
        assert record is not None
        assert record.content == "Loves coffee"

    def test_reinforce_same_content(self, db_session):
        writer = MemoryWriteService(db_session)
        p1 = _make_proposal("user.preference.coffee", "Loves coffee")
        result1 = writer.write(p1, run_context=_make_ctx())
        assert result1.operation == "create"

        db_session.flush()

        p2 = _make_proposal("user.preference.coffee", "Loves coffee")
        result2 = writer.write(p2, run_context=_make_ctx("thread-2", "trace-2"))

        assert result2.operation == "reinforce"
        assert result2.memory_id == result1.memory_id

    def test_supersede_single_key(self, db_session):
        writer = MemoryWriteService(db_session)
        p1 = _make_proposal("user.display_name", "Alice")
        result1 = writer.write(p1, run_context=_make_ctx())
        assert result1.operation == "create"
        db_session.flush()

        p2 = _make_proposal("user.display_name", "Bob")
        result2 = writer.write(p2, run_context=_make_ctx("thread-2", "trace-2"))

        assert result2.operation in ("supersede", "revise")
        assert result2.superseded_ids
        old_id = result2.superseded_ids[0]
        assert old_id == result1.memory_id

        store = MemoryStore(db_session)
        old = store.get_by_id(old_id)
        assert old.validity_state == "superseded"
        assert old.superseded_by == result2.memory_id

    def test_multi_key_allows_multiple(self, db_session):
        writer = MemoryWriteService(db_session)
        p1 = _make_proposal("user.preference.color", "Blue")
        result1 = writer.write(p1, run_context=_make_ctx())
        assert result1.written
        db_session.flush()

        p2 = _make_proposal("user.preference.color", "Green")
        result2 = writer.write(p2, run_context=_make_ctx("thread-2", "trace-2"))
        assert result2.written
        # Different content → create new multi record
        assert result2.operation == "create"
        assert result2.memory_id != result1.memory_id

    def test_multi_key_dedup_same_value(self, db_session):
        writer = MemoryWriteService(db_session)
        p1 = _make_proposal("user.preference.color", "Blue")
        result1 = writer.write(p1, run_context=_make_ctx())
        db_session.flush()

        p2 = _make_proposal("user.preference.color", "Blue")
        result2 = writer.write(p2, run_context=_make_ctx("thread-2", "trace-2"))
        # Same content → reinforce
        assert result2.operation == "reinforce"

    def test_forget(self, db_session):
        writer = MemoryWriteService(db_session)
        p1 = _make_proposal("user.preference.temp", "Temp memory")
        result1 = writer.write(p1, run_context=_make_ctx())
        db_session.flush()

        result2 = writer.forget(result1.memory_id, reason="test", run_context=_make_ctx("thread-3", "trace-3"))
        assert result2.written

        store = MemoryStore(db_session)
        record = store.get_by_id(result1.memory_id)
        assert record.lifecycle_state == LifecycleState.FORGOTTEN.value
        assert "forgotten:" in record.content

    def test_event_and_outbox_written(self, db_session, monkeypatch):
        """Phase 0.5B: capability flags control projection outbox; enable for test."""
        from aiive.db.models import Event, OutboxJob
        from aiive.memory.recall_config import get_projection_capabilities
        caps = get_projection_capabilities()
        monkeypatch.setattr(caps, "vector_projection_enabled", True)

        writer = MemoryWriteService(db_session)
        proposal = _make_proposal("user.preference.drink", "Tea")
        result = writer.write(proposal, run_context=_make_ctx())

        assert result.written

        # Event recorded
        events = (
            db_session.query(Event)
            .filter(Event.event_type == "memory.created")
            .all()
        )
        assert len(events) >= 1

        # Outbox jobs enqueued
        jobs = (
            db_session.query(OutboxJob)
            .filter(OutboxJob.trace_id == result.memory_id)
            .all()
        )
        assert len(jobs) >= 1, f"Expected at least 1 outbox job, got {len(jobs)}"
        job_types = {j.job_type for j in jobs}
        assert "memory_vector_upsert" in job_types, f"Got job_types: {job_types}"

    def test_evidence_table_written(self, db_session):
        writer = MemoryWriteService(db_session)
        proposal = _make_proposal("user.preference.sport", "Running")
        result = writer.write(proposal, run_context=_make_ctx())

        evidence = (
            db_session.query(MemoryEvidence)
            .filter(MemoryEvidence.memory_id == result.memory_id)
            .all()
        )
        assert len(evidence) >= 1
        assert evidence[0].source_type == "user_message"

    def test_proposal_persisted(self, db_session):
        writer = MemoryWriteService(db_session)
        proposal = _make_proposal("user.preference.food", "Sushi")
        result = writer.write(proposal, run_context=_make_ctx())

        proposals = (
            db_session.query(MemoryProposalModel)
            .filter(MemoryProposalModel.proposal_id == proposal.proposal_id)
            .all()
        )
        assert len(proposals) == 1
        assert proposals[0].final_operation == result.operation
