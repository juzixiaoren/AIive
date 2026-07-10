"""Test canonical MemoryType enums, scope validation, and type mapping."""
import pytest

from aiive.memory.memory_types import (
    CANONICAL_TYPES,
    MemoryType,
    MemoryProposal,
    EvidenceItem,
    ScopeType,
    TrustLevel,
    Stability,
    LifecycleState,
    ValidityState,
    LineageOperation,
    LEGACY_TYPE_MAP,
    is_canonical_type,
    validate_scope,
)


class TestCanonicalTypes:
    def test_canonical_types_present(self):
        expected = {
            "user_profile", "project", "episodic", "procedural",
            "agent_self", "policy", "environment", "knowledge",
        }
        assert set(MemoryType) == expected

    def test_is_canonical_type(self):
        assert is_canonical_type("user_profile")
        assert is_canonical_type("knowledge")
        assert not is_canonical_type("preference")
        assert not is_canonical_type("routine")
        assert not is_canonical_type("invalid_type")

    def test_legacy_map(self):
        assert LEGACY_TYPE_MAP["preference"] == "user_profile"
        assert LEGACY_TYPE_MAP["routine"] == "user_profile"
        assert LEGACY_TYPE_MAP["habit"] == "user_profile"
        assert LEGACY_TYPE_MAP["schedule"] == "user_profile"
        assert LEGACY_TYPE_MAP["name"] == "user_profile"
        assert LEGACY_TYPE_MAP["fact"] == "knowledge"
        assert LEGACY_TYPE_MAP["user_profile"] == "user_profile"


class TestScopeValidation:
    def test_global_requires_null_id(self):
        validate_scope("global", None)
        validate_scope("global", "")

    def test_global_rejects_non_null_id(self):
        with pytest.raises(ValueError):
            validate_scope("global", "some_id")

    def test_project_requires_id(self):
        with pytest.raises(ValueError):
            validate_scope("project", None)
        with pytest.raises(ValueError):
            validate_scope("project", "")
        validate_scope("project", "my_project")


class TestEnums:
    def test_lifecycle_state(self):
        assert LifecycleState.CANDIDATE.value == "candidate"
        assert LifecycleState.ACTIVE.value == "active"
        assert LifecycleState.SLEEPING.value == "sleeping"
        assert LifecycleState.ARCHIVED.value == "archived"
        assert LifecycleState.FORGOTTEN.value == "forgotten"

    def test_validity_state(self):
        assert ValidityState.VALID.value == "valid"
        assert ValidityState.SUPERSEDED.value == "superseded"
        assert ValidityState.CONTRADICTED.value == "contradicted"
        assert ValidityState.EXPIRED.value == "expired"

    def test_lineage_operation(self):
        assert LineageOperation.CREATE.value == "create"
        assert LineageOperation.REINFORCE.value == "reinforce"
        assert LineageOperation.SUPERSEDE.value == "supersede"
        assert LineageOperation.FORGET.value == "forget"

    def test_trust_level(self):
        assert TrustLevel.TRUSTED.value == "trusted"
        assert TrustLevel.UNTRUSTED.value == "untrusted"

    def test_stability(self):
        assert Stability.STABLE.value == "stable"
        assert Stability.CONTEXTUAL.value == "contextual"
        assert Stability.VOLATILE.value == "volatile"


class TestMemoryProposal:
    def test_create_proposal(self):
        p = MemoryProposal(
            memory_type="user_profile",
            canonical_key="user.display_name",
            content="B",
            evidence=[
                EvidenceItem(source_type="user_message", trust_level="trusted"),
            ],
        )
        p.compute_request_idempotency()
        assert p.idempotency_key != ""
        assert p.compute_content_hash() != ""

    def test_non_canonical_type_still_stored(self):
        """Proposal stores whatever type string; validation is by Gate."""
        p = MemoryProposal(memory_type="old_type", canonical_key="test", content="x")
        assert p.memory_type == "old_type"
