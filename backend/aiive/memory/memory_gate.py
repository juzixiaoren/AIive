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
from aiive.memory.memory_policy import AUTHORITY_RELATIONS, MemoryPolicyEngine
from aiive.memory.memory_types import (
    MemoryProposal,
    Sensitivity,
    MemoryType,
)


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
        self._policy = MemoryPolicyEngine()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def decide(self, proposal: MemoryProposal) -> GateDecision:
        """Evaluate a MemoryProposal and return a GateDecision.

        按顺序执行：类型与敏感度校验、统一来源权威、持久性、固定保留、
        临时记忆期限、置信度和 evidence 完整性检查。
        """
        # Rule 1: Type validation
        if proposal.memory_type not in {t.value for t in MemoryType}:
            return GateDecision(
                decision="reject",
                reason=f"Unknown memory_type: {proposal.memory_type}",
                blocked_reason="unknown_memory_type",
            )

        if proposal.sensitivity not in {item.value for item in Sensitivity}:
            return GateDecision(
                decision="reject",
                reason=f"未知记忆敏感度: {proposal.sensitivity}",
                blocked_reason="unknown_sensitivity",
            )

        if proposal.evidence:
            # relation="derived_from" 的证据仅作 provenance，不参与权威判定
            # （如 assistant 回复事件）。过滤后若无任何权威证据，decide_authority
            # 对空列表返回 forbidden → 维持拒绝（保护空权威场景）。
            authority = self._policy.decide_authority(
                proposal.memory_type,
                [
                    item.source_type for item in proposal.evidence
                    if item.relation in AUTHORITY_RELATIONS
                ],
            )
            if not authority.allowed:
                return GateDecision(
                    decision="reject",
                    reason=f"证据来源无写入权威: {authority.reason_code}",
                    blocked_reason=authority.reason_code,
                )

        # Rule 2: Durability — non-durable with no future value → reject
        if not proposal.durable:
            return GateDecision(
                decision="reject",
                reason="Non-durable memory (durable=False): no long-term retention value",
                blocked_reason="blocked_by_durability",
            )

        # Rule 6: Pinned only allowed via user_required
        if (proposal.retention_policy == "pinned"
                and proposal.execution_mode != "user_required"):
            return GateDecision(
                decision="reject",
                reason="retention_policy=pinned requires execution_mode=user_required",
                blocked_reason="blocked_pinned_not_user_required",
            )

        # Rule 7: Ephemeral must have valid_to > valid_from
        if proposal.retention_policy == "ephemeral":
            if proposal.valid_to is None:
                return GateDecision(
                    decision="reject",
                    reason="retention_policy=ephemeral requires valid_to (no indefinite ephemeral)",
                    blocked_reason="blocked_ephemeral_no_ttl",
                )
            # valid_from will be set to now at write time, but if proposal already
            # carries a valid_to, we verify it's in the future
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            if proposal.valid_to <= now:
                return GateDecision(
                    decision="reject",
                    reason=f"valid_to={proposal.valid_to} is not in the future",
                    blocked_reason="blocked_ephemeral_expired_ttl",
                )

        # Rule 8: Confidence threshold → candidate
        if proposal.confidence < 0.7:
            return GateDecision(
                decision="candidate",
                reason=f"Confidence {proposal.confidence} below 0.7 threshold",
            )

        # Rule 9: No evidence → candidate
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
