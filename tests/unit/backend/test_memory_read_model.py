"""Test MemoryReadModel context building with both exact keys and dynamic patterns."""
from aiive.memory.memory_store import MemoryStore
from aiive.memory.memory_read_model import MemoryReadModel, RuntimeIdentity
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

    def test_dynamic_memories_not_in_deprecated_batch(self, db_session):
        """V2: dynamic (pattern-key) memories are retrieved query-aware via
        AutomaticRecallEngine, not injected as a flat list.
        """
        store = MemoryStore(db_session)
        _make_and_store(store, "user.preference.color", "Blue", "user_profile")
        _make_and_store(store, "user.routine.morning", "Wake at 7am", "user_profile")
        _make_and_store(store, "user.habit.reading", "Reads nightly", "user_profile")
        _make_and_store(store, "knowledge.weather", "HK is humid", "knowledge")
        db_session.flush()

        model = MemoryReadModel(store)
        # V2: resolve_identity + resolve_policies replace deprecated build_context().
        identity = model.resolve_identity()
        assert isinstance(identity, RuntimeIdentity)

        # The same memories ARE retrievable via the new query-aware recall path.
        from aiive.memory.automatic_recall import AutomaticRecallEngine
        from aiive.memory.recall_config import RecallConfig
        from aiive.memory.recall_models import MemoryRecallRequest, ScopeContext

        engine = AutomaticRecallEngine(db_session, RecallConfig())
        pack, _ = engine.recall(MemoryRecallRequest(
            query="morning routine wake and nightly reading", scope_context=ScopeContext(thread_id="t"),
            top_k=8, token_budget=2000,
        ))
        contents = {it.content for it in pack.items}
        assert "Wake at 7am" in contents
        assert "Reads nightly" in contents

    def test_policy_in_context(self, db_session):
        store = MemoryStore(db_session)
        _make_and_store(store, "policy.no_external_tools", "No external MCP installs", "policy")
        db_session.flush()

        model = MemoryReadModel(store)
        policies = model.resolve_policies()
        all_content = {p["content"] for p in policies}
        assert "No external MCP installs" in all_content

    def test_empty_context_no_crash(self, db_session):
        store = MemoryStore(db_session)
        model = MemoryReadModel(store)
        identity = model.resolve_identity()
        policies = model.resolve_policies()
        # No memories stored — identity empty, policies empty, no crash.
        assert identity.is_empty()
        assert policies == []
