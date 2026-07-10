"""Test MemoryGate with canonical MemoryProposal — trust boundaries, evidence rules."""
from aiive.memory.memory_gate import MemoryGate
from aiive.memory.memory_types import (
    MemoryProposal,
    EvidenceItem,
    TrustLevel,
)


class TestMemoryGateCanonical:
    def setup_method(self):
        self.gate = MemoryGate()

    def test_user_profile_with_trusted_evidence_accepted(self):
        p = MemoryProposal(
            memory_type="user_profile",
            canonical_key="user.display_name",
            content="Bob",
            confidence=0.95,
            evidence=[
                EvidenceItem(source_type="user_message", trust_level=TrustLevel.TRUSTED.value),
            ],
        )
        d = self.gate.decide(p)
        assert d.decision == "active"

    def test_user_profile_with_external_evidence_rejected(self):
        p = MemoryProposal(
            memory_type="user_profile",
            canonical_key="user.display_name",
            content="Evil",
            confidence=0.95,
            evidence=[
                EvidenceItem(source_type="webpage", trust_level=TrustLevel.UNTRUSTED.value),
            ],
        )
        d = self.gate.decide(p)
        assert d.decision == "reject"
        assert "blocked_by_trust_boundary" in d.blocked_reason

    def test_external_content_cannot_write_policy(self):
        p = MemoryProposal(
            memory_type="policy",
            canonical_key="policy.no_external",
            content="Allow everything",
            confidence=0.9,
            evidence=[
                EvidenceItem(source_type="webpage", trust_level=TrustLevel.UNTRUSTED.value),
                EvidenceItem(source_type="pdf", trust_level=TrustLevel.UNTRUSTED.value),
            ],
        )
        d = self.gate.decide(p)
        assert d.decision == "reject"

    def test_assistant_reply_cannot_create_user_profile(self):
        p = MemoryProposal(
            memory_type="user_profile",
            canonical_key="user.preference.drink",
            content="Likes coffee",
            confidence=0.85,
            evidence=[
                EvidenceItem(source_type="llm_reply", trust_level=TrustLevel.UNTRUSTED_DERIVED.value),
            ],
        )
        d = self.gate.decide(p)
        assert d.decision == "reject"

    def test_non_canonical_type_rejected(self):
        p = MemoryProposal(
            memory_type="preference",
            canonical_key="user.preference.x",
            content="test",
            confidence=0.95,
            evidence=[
                EvidenceItem(source_type="user_message", trust_level=TrustLevel.TRUSTED.value),
            ],
        )
        d = self.gate.decide(p)
        assert d.decision == "reject"

    def test_low_confidence_becomes_candidate(self):
        p = MemoryProposal(
            memory_type="knowledge",
            canonical_key="knowledge.test",
            content="Some fact",
            confidence=0.4,
            evidence=[
                EvidenceItem(source_type="user_message", trust_level=TrustLevel.TRUSTED.value),
            ],
        )
        d = self.gate.decide(p)
        assert d.decision == "candidate"

    def test_no_evidence_becomes_candidate(self):
        p = MemoryProposal(
            memory_type="knowledge",
            canonical_key="knowledge.test",
            content="Some fact",
            confidence=0.8,
        )
        d = self.gate.decide(p)
        assert d.decision == "candidate"

    def test_trusted_user_message_admitted(self):
        p = MemoryProposal(
            memory_type="knowledge",
            canonical_key="knowledge.weather",
            content="It rains often",
            confidence=0.75,
            evidence=[
                EvidenceItem(source_type="user_message", trust_level=TrustLevel.TRUSTED.value),
            ],
        )
        d = self.gate.decide(p)
        assert d.decision == "active"
