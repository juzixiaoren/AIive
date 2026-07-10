"""MemoryGate: structured memory admission policy.

Decides whether a MemoryProposal should be accepted (active), queued (candidate),
or rejected — based on trust boundaries, evidence quality, and schema conformance.

Key changes from V20:
- No longer depends on execution_mode or should_execute.
- Uses per-evidence trust_level instead of single evidence_source.
- External content blocked from sensitive memory types.
- Assistant reply cannot independently reinforce user profiles.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiive.memory.memory_key_registry import MemoryKeyRegistry, get_memory_key_registry
from aiive.memory.memory_types import (
    MemoryProposal,
    TrustLevel,
    MemoryType,
)


# Memory types that must NOT come from untrusted sources
TRUST_REQUIRED_MEMORY_TYPES: frozenset[str] = frozenset({
    MemoryType.USER_PROFILE.value,
    MemoryType.AGENT_SELF.value,
    MemoryType.POLICY.value,
})

# Evidence source types that are considered "trusted" (direct user input)
TRUSTED_SOURCE_TYPES: frozenset[str] = frozenset({
    "user_message",
    "user_command",
})

# External (untrusted) evidence source types
EXTERNAL_SOURCE_TYPES: frozenset[str] = frozenset({
    "webpage",
    "pdf",
    "email",
    "code_comment",
    "retrieved_knowledge",
    "tool_observation",
    "mcp_description",
    "llm_reply",
})


# ---------------------------------------------------------------------------
# Gate Decision
# ---------------------------------------------------------------------------

@dataclass
class GateDecision:
    """MemoryGate admission decision."""
    decision: str  # "active" | "candidate" | "reject"
    reason: str = ""
    blocked_reason: str = ""
    requires_confirmation: bool = False
    trace_id: str = ""
    source_event_id: str = ""


# ---------------------------------------------------------------------------
# MemoryGate
# ---------------------------------------------------------------------------

class MemoryGate:
    """Admission controller: evaluates MemoryProposal for write eligibility.

    Pure decision logic — does not write, does not query DB.
    All existing-memory checks belong to ConflictResolver, not here.
    """

    def __init__(self, registry: MemoryKeyRegistry | None = None) -> None:
        self._registry: MemoryKeyRegistry = registry or get_memory_key_registry()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def decide(self, proposal: MemoryProposal) -> GateDecision:
        """Evaluate a MemoryProposal and return a GateDecision.

        Rules applied in order:
        1. Type validation (must be canonical)
        2. Trust boundary (sensitive types must have trusted evidence)
        3. External content restrictions
        4. Assistant reply restrictions
        5. Confidence threshold → candidate
        6. Evidence quality → candidate
        7. Default: active
        """
        # Rule 1: Type validation
        if proposal.memory_type not in {t.value for t in MemoryType}:
            return GateDecision(
                decision="reject",
                reason=f"Unknown memory_type: {proposal.memory_type}",
                blocked_reason="unknown_memory_type",
            )

        # Rule 2: Trust boundary — sensitive types
        if proposal.memory_type in TRUST_REQUIRED_MEMORY_TYPES:
            if not self._has_trusted_user_evidence(proposal):
                return GateDecision(
                    decision="reject",
                    reason=(
                        f"Sensitive memory type '{proposal.memory_type}' "
                        f"requires trusted user evidence"
                    ),
                    blocked_reason="blocked_by_trust_boundary",
                )

        # Rule 3: External content cannot produce user_profile/policy/agent_self
        if self._has_only_external_evidence(proposal):
            if proposal.memory_type in TRUST_REQUIRED_MEMORY_TYPES:
                return GateDecision(
                    decision="reject",
                    reason=(
                        f"External content cannot write '{proposal.memory_type}'"
                    ),
                    blocked_reason="blocked_by_external_source",
                )

        # Rule 4: Assistant reply cannot independently create/strengthen user profile
        if self._is_pure_assistant_evidence(proposal):
            if proposal.memory_type == MemoryType.USER_PROFILE.value:
                return GateDecision(
                    decision="reject",
                    reason="Assistant reply cannot independently create user profile",
                    blocked_reason="blocked_by_assistant_source",
                )

        # Rule 5: Confidence threshold → candidate
        if proposal.confidence < 0.7:
            return GateDecision(
                decision="candidate",
                reason=f"Confidence {proposal.confidence} below 0.7 threshold",
            )

        # Rule 6: No evidence → candidate
        if not proposal.evidence:
            return GateDecision(
                decision="candidate",
                reason="No evidence provided; queued as candidate",
            )

        # Default: accept
        return GateDecision(
            decision="active",
            reason="All admission checks passed",
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _has_trusted_user_evidence(proposal: MemoryProposal) -> bool:
        """Return True if any evidence item is from a trusted user source."""
        for ev in proposal.evidence:
            if ev.source_type in TRUSTED_SOURCE_TYPES:
                if ev.trust_level in (
                    TrustLevel.TRUSTED.value,
                    TrustLevel.SEMI_TRUSTED.value,
                ):
                    return True
        return False

    @staticmethod
    def _has_only_external_evidence(proposal: MemoryProposal) -> bool:
        """Return True if ALL evidence is from external sources."""
        if not proposal.evidence:
            return False
        return all(
            ev.source_type in EXTERNAL_SOURCE_TYPES
            for ev in proposal.evidence
        )

    @staticmethod
    def _is_pure_assistant_evidence(proposal: MemoryProposal) -> bool:
        """Return True if ALL evidence is from LLM replies only."""
        if not proposal.evidence:
            return False
        return all(
            ev.source_type == "llm_reply"
            for ev in proposal.evidence
        )
