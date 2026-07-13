"""MemoryKeyRegistry: single source of truth for memory key specs.

Every memory key, its canonical type, cardinality, scope, context roles,
conflict policy, and maintenance policy is defined here — nowhere else.

Supports both exact keys (identity/policy) and dynamic patterns (project.*, user.preference.*).
Exact keys have priority over patterns; patterns use longest-match.

agent.runtime_id is NOT a memory key — it belongs to runtime config,
not MemoryRecord. RuntimeIdentity is composed from runtime config + agent identity memory
+ user identity memory, not from arbitrary memory keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiive.memory.memory_types import MemoryType, ScopeType


# ============================================================================
# MemoryKeySpec
# ============================================================================

@dataclass(frozen=True)
class MemoryKeySpec:
    """Immutable specification for a memory key."""

    canonical_key: str
    memory_type: str                     # canonical MemoryType value
    cardinality: str                     # "single" | "multi"
    default_scope: str = ScopeType.GLOBAL.value
    context_roles: tuple[str, ...] = ()  # "runtime_identity", "user_memory", "policy", ...
    conflict_policy: str = "supersede"   # supersede / merge / append / ignore_duplicate
    maintenance_policy: str = "persist"  # persist / decay / expire_7d / expire_30d
    structured_value_schema: dict[str, Any] | None = None
    is_pattern: bool = False             # True for "project.*" style patterns
    core_memory_role: str | None = None  # "core.human_identity" / "core.interaction_defaults" /
                                         # "core.agent_persona" — only these keys may feed Core Memory


# ============================================================================
# KeyPattern for dynamic keys
# ============================================================================

@dataclass(frozen=True)
class KeyPattern:
    """A pattern for dynamic memory keys (e.g., project.*, user.preference.*).

    Patterns are matched by prefix with priority given to longer matches.
    """
    prefix: str                          # e.g. "user.preference."
    memory_type: str
    cardinality: str
    default_scope: str = ScopeType.GLOBAL.value
    context_roles: tuple[str, ...] = ()
    conflict_policy: str = "append"
    maintenance_policy: str = "persist"
    structured_value_schema: dict[str, Any] | None = None
    core_memory_role: str | None = None

    def match(self, key: str) -> bool:
        """Return True if key starts with this pattern's prefix."""
        return key.startswith(self.prefix)

    def match_length(self, key: str) -> int:
        """Length of the matched prefix, used for longest-match priority."""
        return len(self.prefix) if key.startswith(self.prefix) else 0


# ============================================================================
# MemoryKeyRegistry
# ============================================================================

class MemoryKeyRegistry:
    """Central registry for all memory key specifications.

    Usage:
        registry = get_memory_key_registry()
        spec = registry.resolve("user.display_name")
        # → MemoryKeySpec(cardinality="single", ...)
        spec = registry.resolve("project.aiive.backend_stack")
        # → resolved via pattern "project.*"
    """

    def __init__(self) -> None:
        self._exact: dict[str, MemoryKeySpec] = {}
        self._patterns: list[KeyPattern] = []
        self._init_keys()
        self._init_patterns()

    # ------------------------------------------------------------------
    # Exact keys (strict registration)
    # ------------------------------------------------------------------

    def _init_keys(self) -> None:
        exacts: list[MemoryKeySpec] = [
            # -- User identity -- (also feeds Core Memory: human_identity)
            MemoryKeySpec(
                canonical_key="user.display_name",
                memory_type=MemoryType.USER_PROFILE.value,
                cardinality="single",
                context_roles=("runtime_identity",),
                conflict_policy="supersede",
                maintenance_policy="persist",
                core_memory_role="core.human_identity",
            ),
            MemoryKeySpec(
                canonical_key="user.name",
                memory_type=MemoryType.USER_PROFILE.value,
                cardinality="single",
                context_roles=("runtime_identity",),
                conflict_policy="supersede",
                maintenance_policy="persist",
                core_memory_role="core.human_identity",
            ),
            # -- User preference (exact single keys) -- (feeds Core Memory: interaction_defaults)
            MemoryKeySpec(
                canonical_key="user.preference.response_style",
                memory_type=MemoryType.USER_PROFILE.value,
                cardinality="single",
                context_roles=("runtime_identity",),
                conflict_policy="supersede",
                maintenance_policy="persist",
                core_memory_role="core.interaction_defaults",
            ),
            MemoryKeySpec(
                canonical_key="user.preference.default_language",
                memory_type=MemoryType.USER_PROFILE.value,
                cardinality="single",
                context_roles=("runtime_identity",),
                conflict_policy="supersede",
                maintenance_policy="persist",
                core_memory_role="core.interaction_defaults",
            ),
            # -- Agent identity -- (feeds Core Memory: agent_persona)
            MemoryKeySpec(
                canonical_key="agent.display_name",
                memory_type=MemoryType.AGENT_SELF.value,
                cardinality="single",
                context_roles=("runtime_identity",),
                conflict_policy="supersede",
                maintenance_policy="persist",
                core_memory_role="core.agent_persona",
            ),
            MemoryKeySpec(
                canonical_key="agent.persona.tone",
                memory_type=MemoryType.AGENT_SELF.value,
                cardinality="single",
                context_roles=("runtime_identity",),
                conflict_policy="supersede",
                maintenance_policy="persist",
                core_memory_role="core.agent_persona",
            ),
            MemoryKeySpec(
                canonical_key="agent.persona.relationship",
                memory_type=MemoryType.AGENT_SELF.value,
                cardinality="single",
                context_roles=("runtime_identity",),
                conflict_policy="supersede",
                maintenance_policy="persist",
                core_memory_role="core.agent_persona",
            ),
            # -- Explicit Core Memory projection keys (Agent may write directly) --
            MemoryKeySpec(
                canonical_key="core.human_identity",
                memory_type=MemoryType.USER_PROFILE.value,
                cardinality="single",
                conflict_policy="supersede",
                maintenance_policy="persist",
                core_memory_role="core.human_identity",
            ),
            MemoryKeySpec(
                canonical_key="core.interaction_defaults",
                memory_type=MemoryType.USER_PROFILE.value,
                cardinality="single",
                conflict_policy="supersede",
                maintenance_policy="persist",
                core_memory_role="core.interaction_defaults",
            ),
            MemoryKeySpec(
                canonical_key="core.agent_persona",
                memory_type=MemoryType.AGENT_SELF.value,
                cardinality="single",
                conflict_policy="supersede",
                maintenance_policy="persist",
                core_memory_role="core.agent_persona",
            ),
        ]
        for ks in exacts:
            self._exact[ks.canonical_key] = ks

    # ------------------------------------------------------------------
    # Dynamic patterns (longest-match priority)
    # ------------------------------------------------------------------

    def _init_patterns(self) -> None:
        raw: list[KeyPattern] = [
            KeyPattern(
                prefix="user.preference.",
                memory_type=MemoryType.USER_PROFILE.value,
                cardinality="multi",
                context_roles=("user_memory",),
                conflict_policy="append",
                maintenance_policy="persist",
            ),
            KeyPattern(
                prefix="user.routine.",
                memory_type=MemoryType.USER_PROFILE.value,
                cardinality="multi",
                context_roles=("user_memory",),
                conflict_policy="append",
                maintenance_policy="decay",
            ),
            KeyPattern(
                prefix="user.habit.",
                memory_type=MemoryType.USER_PROFILE.value,
                cardinality="multi",
                context_roles=("user_memory",),
                conflict_policy="append",
                maintenance_policy="decay",
            ),
            KeyPattern(
                prefix="user.schedule.",
                memory_type=MemoryType.USER_PROFILE.value,
                cardinality="multi",
                context_roles=("user_memory",),
                conflict_policy="append",
                maintenance_policy="expire_7d",
            ),
            KeyPattern(
                prefix="project.",
                memory_type=MemoryType.PROJECT.value,
                cardinality="multi",
                default_scope=ScopeType.PROJECT.value,
                context_roles=("user_memory",),
                conflict_policy="append",
                maintenance_policy="persist",
            ),
            KeyPattern(
                prefix="policy.",
                memory_type=MemoryType.POLICY.value,
                cardinality="multi",
                context_roles=("policy",),
                conflict_policy="supersede",
                maintenance_policy="persist",
            ),
            KeyPattern(
                prefix="environment.",
                memory_type=MemoryType.ENVIRONMENT.value,
                cardinality="multi",
                default_scope=ScopeType.ENVIRONMENT.value,
                context_roles=(),
                conflict_policy="append",
                maintenance_policy="persist",
            ),
            KeyPattern(
                prefix="knowledge.",
                memory_type=MemoryType.KNOWLEDGE.value,
                cardinality="multi",
                context_roles=("user_memory",),
                conflict_policy="append",
                maintenance_policy="persist",
            ),
            KeyPattern(
                prefix="episodic.",
                memory_type=MemoryType.EPISODIC.value,
                cardinality="multi",
                context_roles=(),
                conflict_policy="append",
                maintenance_policy="expire_30d",
            ),
        ]
        # Sort by length descending for longest-match priority
        raw.sort(key=lambda p: len(p.prefix), reverse=True)
        self._patterns = raw

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(self, key: str) -> MemoryKeySpec | None:
        """Resolve a memory key to its spec.

        Exact match has highest priority, then longest-match pattern.
        """
        exact = self._exact.get(key)
        if exact is not None:
            return exact

        best: tuple[int, KeyPattern | None] = (0, None)
        for pat in self._patterns:
            ml = pat.match_length(key)
            if ml > best[0]:
                best = (ml, pat)

        if best[1] is None:
            return None

        pat = best[1]
        return MemoryKeySpec(
            canonical_key=key,
            memory_type=pat.memory_type,
            cardinality=pat.cardinality,
            default_scope=pat.default_scope,
            context_roles=pat.context_roles,
            conflict_policy=pat.conflict_policy,
            maintenance_policy=pat.maintenance_policy,
            structured_value_schema=pat.structured_value_schema,
            is_pattern=True,
        )

    def is_single_cardinality(self, key: str) -> bool:
        """Return True if key has single cardinality."""
        spec = self._exact.get(key)
        if spec is not None:
            return spec.cardinality == "single"
        resolved = self.resolve(key)
        if resolved is not None:
            return resolved.cardinality == "single"
        return False

    def get_context_role_keys(self, role: str) -> list[str]:
        """Return all exact keys registered for a context role."""
        result: list[str] = []
        for key, spec in self._exact.items():
            if role in spec.context_roles:
                result.append(key)
        return result

    def get_context_role_patterns(self, role: str) -> list[str]:
        """Return pattern prefixes whose context_roles include `role`.

        Used by query-based resolution (e.g. `policy.*` records) so we can match
        records without a full table scan.
        """
        result: list[str] = []
        for p in self._patterns:
            if role in p.context_roles:
                result.append(p.prefix)
        return result

    def get_core_memory_keys(self) -> dict[str, str]:
        """Return mapping canonical_key -> core_memory_role for all registered keys.

        Only keys explicitly declaring a core_memory_role may feed the Core
        Memory projection. Dynamic patterns are intentionally excluded so the
        projection stays small and stable.
        """
        result: dict[str, str] = {}
        for key, spec in self._exact.items():
            if spec.core_memory_role:
                result[key] = spec.core_memory_role
        return result

    def get_context_role_types(self, role: str) -> list[str]:
        """Return memory_types used by any key/pattern with the given context role."""
        types: set[str] = set()
        for ks in self._exact.values():
            if role in ks.context_roles:
                types.add(ks.memory_type)
        for pat in self._patterns:
            if role in pat.context_roles:
                types.add(pat.memory_type)
        return list(types)

    def get_context_role_prefixes(self, role: str) -> list[str]:
        """Return dynamic key prefixes registered for a context role."""
        prefixes: list[str] = []
        for pat in self._patterns:
            if role in pat.context_roles:
                prefixes.append(pat.prefix)
        return prefixes

    def list_exact_keys(self) -> list[str]:
        """Return all registered exact keys."""
        return list(self._exact.keys())


# ============================================================================
# Singleton
# ============================================================================

_registry: MemoryKeyRegistry | None = None


def get_memory_key_registry() -> MemoryKeyRegistry:
    """Return the singleton MemoryKeyRegistry."""
    global _registry
    if _registry is None:
        _registry = MemoryKeyRegistry()
    return _registry
