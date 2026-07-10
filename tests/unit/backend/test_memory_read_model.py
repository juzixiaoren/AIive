"""Test MemoryReadModel context building with both exact keys and dynamic patterns."""
from aiive.memory.memory_store import MemoryStore
from aiive.memory.memory_read_model import MemoryReadModel
from aiive.memory.memory_types import (
    MemoryProposal,
    EvidenceItem,
    LifecycleState,
    TrustLevel,
)


def _make_and_store(store, key, content, mem_type="user_profile"):
    p = MemoryProposal(
        memory_type=mem_type,
        canonical_key=key,
        content=content,
        confidence=0.95,
        importance=0.8,
        evidence=[EvidenceItem(source_type="user_message", trust_level=TrustLevel.TRUSTED.value)],
    )
    p.compute_request_idempotency()
    return store.create_record(p, lifecycle_state=LifecycleState.ACTIVE.value)


class TestMemoryReadModel:
    def test_runtime_identity_resolved(self, db_session):
        store = MemoryStore(db_session)
        _make_and_store(store, "agent.display_name", "HelperBot", "agent_self")
        _make_and_store(store, "user.display_name", "Alice", "user_profile")
        _make_and_store(store, "agent.persona.relationship", "trusted assistant", "agent_self")
        db_session.flush()

        model = MemoryReadModel(store)
        identity = model.resolve_identity()
        assert identity.agent_display_name == "HelperBot"
        assert identity.user_display_name == "Alice"
        assert identity.relationship_style == "trusted assistant"

    def test_dynamic_memories_in_context(self, db_session):
        """Dynamic memories with pattern keys should appear in build_context()."""
        store = MemoryStore(db_session)
        _make_and_store(store, "user.preference.color", "Blue", "user_profile")
        _make_and_store(store, "user.routine.morning", "Wake at 7am", "user_profile")
        _make_and_store(store, "user.habit.reading", "Reads nightly", "user_profile")
        _make_and_store(store, "knowledge.weather", "HK is humid", "knowledge")
        db_session.flush()

        model = MemoryReadModel(store)
        ctx = model.build_context()

        # user_memories should NOT be empty
        assert len(ctx.user_memories) > 0, (
            "Dynamic pattern memories should appear in context, but got empty list. "
            "Context role type-based fallback must work."
        )

        contents = {m["content"] for m in ctx.user_memories}
        assert "Blue" in contents
        assert "Wake at 7am" in contents
        assert "Reads nightly" in contents

    def test_policy_in_context(self, db_session):
        store = MemoryStore(db_session)
        _make_and_store(store, "policy.no_external_tools", "No external MCP installs", "policy")
        db_session.flush()

        model = MemoryReadModel(store)
        ctx = model.build_context()
        # Policy records are retrieved via Retriever into user_memories (active+valid global)
        all_content = {m["content"] for m in ctx.user_memories}
        assert "No external MCP installs" in all_content

    def test_empty_context_no_crash(self, db_session):
        store = MemoryStore(db_session)
        model = MemoryReadModel(store)
        ctx = model.build_context()
        # Should not crash, just return empty
        assert ctx.user_memories == []
        assert ctx.policies == []
