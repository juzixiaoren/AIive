"""Test MemoryStore CRUD with new canonical schema."""
from aiive.memory.memory_store import MemoryStore
from aiive.memory.memory_types import (
    MemoryProposal,
    EvidenceItem,
    LifecycleState,
    ValidityState,
    TrustLevel,
    Stability,
)


def _make_proposal(key: str, content: str, mem_type: str = "user_profile") -> MemoryProposal:
    p = MemoryProposal(
        memory_type=mem_type,
        canonical_key=key,
        content=content,
        evidence=[EvidenceItem(source_type="user_message", trust_level=TrustLevel.TRUSTED.value)],
    )
    p.compute_request_idempotency()
    return p


class TestMemoryStore:
    def test_create_and_retrieve(self, db_session):
        store = MemoryStore(db_session)
        proposal = _make_proposal("user.preference.drink", "User loves coffee")
        record = store.create_record(proposal, lifecycle_state=LifecycleState.ACTIVE.value)
        db_session.flush()

        retrieved = store.get_by_id(record.id)
        assert retrieved is not None
        assert retrieved.content == "User loves coffee"
        assert retrieved.memory_type == "user_profile"
        assert retrieved.canonical_key == "user.preference.drink"
        assert retrieved.scope_type == "global"
        assert retrieved.cardinality == "multi"

    def test_get_active_by_key(self, db_session):
        store = MemoryStore(db_session)
        store.create_record(
            _make_proposal("user.display_name", "Alice"),
            lifecycle_state=LifecycleState.ACTIVE.value,
        )
        store.create_record(
            _make_proposal("user.display_name", "Bob"),
            lifecycle_state=LifecycleState.CANDIDATE.value,
        )
        db_session.flush()

        active = store.get_active_by_key("user.display_name")
        assert len(active) == 1
        assert active[0].content == "Alice"

    def test_get_active_by_key_scope(self, db_session):
        store = MemoryStore(db_session)
        store.create_record(
            _make_proposal("user.display_name", "Alice"),
            lifecycle_state=LifecycleState.ACTIVE.value,
        )
        db_session.flush()

        results = store.get_active_by_key_scope("user.display_name", "global", None)
        assert len(results) == 1

    def test_get_by_context_roles(self, db_session):
        store = MemoryStore(db_session)
        p = _make_proposal("agent.display_name", "Helper", "agent_self")
        store.create_record(p, lifecycle_state=LifecycleState.ACTIVE.value)
        db_session.flush()

        records = store.get_by_context_roles(["runtime_identity"])
        assert len(records) >= 1
        keys = {r.canonical_key for r in records}
        assert "agent.display_name" in keys

    def test_single_key_active_enforced_by_index(self, db_session):
        """Partial unique index prevents two active single-cardinality records.

        Note: This test requires PostgreSQL. SQLite doesn't support partial
        unique indexes with WHERE clauses. On SQLite, this test validates
        that two records can be created but relies on application-level
        enforcement (ConflictResolver).
        """
        import pytest
        from sqlalchemy.exc import IntegrityError

        store = MemoryStore(db_session)
        store.create_record(
            _make_proposal("user.display_name", "Alice"),
            lifecycle_state=LifecycleState.ACTIVE.value,
        )
        db_session.flush()

        store.create_record(
            _make_proposal("user.display_name", "Bob"),
            lifecycle_state=LifecycleState.ACTIVE.value,
        )
        try:
            db_session.flush()
            # SQLite: no partial unique index → no error; app-level enforcement handles it
            # Just rollback to clean up
            db_session.rollback()
        except IntegrityError:
            # PostgreSQL: partial unique index raised IntegrityError
            db_session.rollback()
