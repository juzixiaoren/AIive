"""V20 MemoryGate: structured memory admission policy.

TRANSFORMED: Removed ALL keyword tables. MemoryGate now decides purely on structured
fields from IntentResult and ExtractedMemory. Natural language understanding
is the responsibility of IntentClassifier and MemoryExtractor, NOT MemoryGate.
"""

from dataclasses import dataclass, field

# Ordered by priority: single-key means at most one active record with this key
SINGLE_KEY_KEYS = frozenset({
    "user.display_name",
    "user.name",
    "agent.display_name",
    "user.preference.response_style",
})

# Keys that always start with a known prefix
KNOWN_KEY_PREFIXES = frozenset({
    "user.display_name", "user.name", "user.preference.",
    "agent.display_name", "agent.persona.",
    "project.",
})

# Intent types eligible for memory admission (others → reject or delegate to Scheduler)
MEMORY_ELIGIBLE = frozenset({
    "memory_update",
    "user_identity_update",
    "agent_identity_update",
    "user_preference_update",
    "agent_persona_update",
    "project_decision_update",
    "policy_memory_update",
})

# Intent types that must be routed to Scheduler, NOT MemoryGate
SCHEDULER_INTENTS = frozenset({
    "reminder_create",
    "routine_create",
    "scheduled_tool_task",
})

# Memory types that must NOT come from untrusted sources
TRUST_REQUIRED_MEMORY_TYPES = frozenset({
    "user_profile", "agent_self", "preference", "policy",
})


# ---------------------------------------------------------------------------
# Structured I/O
# ---------------------------------------------------------------------------

@dataclass
class MemoryGateInput:
    content: str
    user_message: str
    source: str = "auto_extraction"  # "auto_extraction" | "tool_call" | "steward"
    intent_type: str = "unknown"
    execution_mode: str = "explain_only"
    should_execute: bool = False
    evidence_source: str = "trusted_user_message"  # trusted_user_message | untrusted_external_content | tool_result
    extracted_memory_type: str | None = None
    extracted_memory_key: str | None = None
    confidence: float = 0.5
    existing_memory: dict | None = None
    trace_id: str = ""
    source_event_id: str = ""


@dataclass
class MemoryGateDecision:
    decision: str  # "active" | "candidate" | "reject"
    memory_type: str | None = None
    memory_key: str | None = None
    update_mode: str = "create"  # "create" | "upsert" | "supersede" | "ignore"
    confidence: float = 0.5
    reason: str = ""
    blocked_reason: str = ""
    supersede_memory_ids: list[str] = field(default_factory=list)
    requires_user_confirmation: bool = False
    source_event_id: str = ""
    trace_id: str = ""


# ---------------------------------------------------------------------------
# MemoryKeyResolver (deterministic, no NLP)
# ---------------------------------------------------------------------------

class MemoryKeyResolver:
    """Resolves stable memory_key from structured ExtractedMemory hints."""

    @staticmethod
    def resolve(memory_type_hint: str | None, memory_key_hint: str | None,
                content: str) -> str | None:
        # Explicit hint from IntentClassifier takes precedence
        if memory_key_hint and memory_key_hint in KNOWN_KEY_PREFIXES:
            return memory_key_hint
        if memory_key_hint:
            return memory_key_hint

        # Fallback: derive from memory_type if possible
        if memory_type_hint == "user_profile":
            return "user.display_name"
        if memory_type_hint == "agent_self":
            return "agent.display_name"
        if memory_type_hint == "preference":
            return "user.preference.response_style"

        return None


# ---------------------------------------------------------------------------
# MemoryGate
# ---------------------------------------------------------------------------

class MemoryGate:
    """Admission policy: decides on STRUCTURED INPUT only. No NLP, no keywords."""

    def __init__(self):
        self._key_resolver = MemoryKeyResolver()

    # pylint: disable=too-many-return-statements,too-many-branches
    def decide(self, inp: MemoryGateInput) -> MemoryGateDecision:
        """Pure admission decision based on structured fields."""

        # ---- Rule 1: execution_mode gate ----
        if inp.execution_mode != "execute":
            return MemoryGateDecision(
                decision="reject",
                reason="execution_mode is not execute",
                blocked_reason="not_execute",
                trace_id=inp.trace_id,
            )

        # ---- Rule 2: should_execute gate ----
        if not inp.should_execute:
            return MemoryGateDecision(
                decision="reject",
                reason="should_execute is false",
                blocked_reason="should_execute_false",
                trace_id=inp.trace_id,
            )

        # ---- Rule 3: trust boundary ----
        if (inp.evidence_source != "trusted_user_message" and
            inp.extracted_memory_type in TRUST_REQUIRED_MEMORY_TYPES):
            return MemoryGateDecision(
                decision="reject",
                reason=f"untrusted source '{inp.evidence_source}' cannot write {inp.extracted_memory_type}",
                blocked_reason="blocked_by_trust_boundary",
                trace_id=inp.trace_id,
            )

        # ---- Rule 4: intent_type not eligible ----
        if inp.intent_type not in MEMORY_ELIGIBLE:
            if inp.intent_type in SCHEDULER_INTENTS:
                return MemoryGateDecision(
                    decision="reject",
                    reason=f"intent '{inp.intent_type}' should be routed to Scheduler, not MemoryGate",
                    blocked_reason="route_to_scheduler",
                    trace_id=inp.trace_id,
                )
            return MemoryGateDecision(
                decision="reject",
                reason=f"intent_type '{inp.intent_type}' not eligible for memory admission",
                blocked_reason="not_memory_intent",
                trace_id=inp.trace_id,
            )

        # ---- Rule 5: memory_key required for active ----
        memory_key = inp.extracted_memory_key
        if not memory_key or not memory_key.strip():
            return MemoryGateDecision(
                decision="candidate",
                memory_type=inp.extracted_memory_type,
                confidence=inp.confidence,
                reason="no stable memory_key; queued as candidate",
                trace_id=inp.trace_id,
            )

        # ---- Rule 6: confidence threshold ----
        if inp.confidence < 0.7:
            return MemoryGateDecision(
                decision="candidate",
                memory_type=inp.extracted_memory_type,
                memory_key=memory_key,
                confidence=inp.confidence,
                reason=f"confidence {inp.confidence} below 0.7 threshold",
                trace_id=inp.trace_id,
            )

        # ---- Rule 7: supersede existing ----
        if inp.existing_memory and inp.existing_memory.get("id"):
            return MemoryGateDecision(
                decision="active",
                memory_type=inp.extracted_memory_type or inp.existing_memory.get("memory_type"),
                memory_key=memory_key,
                update_mode="supersede",
                confidence=max(inp.confidence, 0.9),
                reason="superseding existing memory with same key",
                supersede_memory_ids=[inp.existing_memory["id"]],
                source_event_id=inp.source_event_id,
                trace_id=inp.trace_id,
            )

        # ---- Rule 8: single-key uniqueness ----
        if memory_key in SINGLE_KEY_KEYS:
            # Single-key: mark as upsert (write service will enforce one active)
            return MemoryGateDecision(
                decision="active",
                memory_type=inp.extracted_memory_type,
                memory_key=memory_key,
                update_mode="upsert",
                confidence=inp.confidence,
                reason=f"single-key memory '{memory_key}' admitted",
                source_event_id=inp.source_event_id,
                trace_id=inp.trace_id,
            )

        # ---- Rule 9: default active ----
        return MemoryGateDecision(
            decision="active",
            memory_type=inp.extracted_memory_type,
            memory_key=memory_key,
            update_mode="create",
            confidence=inp.confidence,
            reason="all checks passed",
            source_event_id=inp.source_event_id,
            trace_id=inp.trace_id,
        )

    # ------------------------------------------------------------------
    # Backward-compatible wrapper (deprecated — production must use decide())
    # ------------------------------------------------------------------

    def decide_str(self, content: str, user_message: str) -> str:
        """DEPRECATED: Use decide(MemoryGateInput) instead.
        Returns 'active', 'candidate', or 'reject' for backward compat only.
        """
        result = self.decide(MemoryGateInput(
            content=content, user_message=user_message,
            execution_mode="explain_only", should_execute=False,
        ))
        return result.decision
