"""MemoryReadModel: deterministic key-based memory reader.

Provides Runtime Identity and Policy resolution using MemoryKeyRegistry
context roles. Does NOT perform full table scans — queries by specific
canonical_keys registered for each context role.

Replaces AgentGraph._get_runtime_identity() and _resolve_memories_for_context().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Sequence
from typing import Any

from aiive.db.models import MemoryRecord
from aiive.memory.memory_store import MemoryStore


@dataclass
class RuntimeIdentity:
    """Resolved runtime identity from active memory records.

    Composed from runtime config + agent identity memory + user identity memory.
    agent.runtime_id is NOT a memory key — it belongs to runtime configuration.
    """
    agent_display_name: str = ""
    agent_runtime_id: str = ""       # from runtime config, NOT memory
    user_display_name: str = ""
    user_name: str = ""
    relationship_style: str = ""
    response_style: str = ""

    def to_dict(self) -> dict[str, str]:
        result: dict[str, str] = {}
        if self.agent_display_name:
            result["agent_display_name"] = self.agent_display_name
        if self.agent_runtime_id:
            result["agent_runtime_id"] = self.agent_runtime_id
        if self.user_display_name:
            result["user_display_name"] = self.user_display_name
        if self.relationship_style:
            result["relationship_style"] = self.relationship_style
        return result

    def is_empty(self) -> bool:
        return not any([
            self.agent_display_name,
            self.user_display_name,
            self.user_name,
        ])


@dataclass
class ReadContext:
    """Context items for injection into LLM system prompt.

    Uses deterministic key-based reading, NOT full active scan.
    """
    runtime_identity: RuntimeIdentity = field(default_factory=RuntimeIdentity)
    policies: list[dict[str, Any]] = field(default_factory=list)
    user_memories: list[dict[str, Any]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return self.runtime_identity.is_empty() and not self.policies and not self.user_memories


class MemoryReadModel:
    """Deterministic memory reader using MemoryKeyRegistry context roles.

    Replaces:
    - AgentGraph._get_runtime_identity() (full scan)
    - AgentGraph._resolve_memories_for_context() (full scan)

    Usage:
        model = MemoryReadModel(MemoryStore(db))
        ctx = model.build_context()
    """

    def __init__(self, store: MemoryStore) -> None:
        self._store: MemoryStore = store

    # ------------------------------------------------------------------
    # Runtime Identity
    # ------------------------------------------------------------------

    def resolve_identity(self, agent_runtime_id: str = "") -> RuntimeIdentity:
        """Resolve runtime identity from registered context role keys.

        Queries only 'runtime_identity' context role keys (exact canonical_keys).
        Does NOT scan all active records.
        """
        identity = RuntimeIdentity(agent_runtime_id=agent_runtime_id)
        records: Sequence[MemoryRecord] = self._store.get_by_context_roles(
            ["runtime_identity"]
        )

        for rec in records:
            if rec.canonical_key == "agent.display_name":
                identity.agent_display_name = rec.content
            elif rec.canonical_key == "user.display_name":
                identity.user_display_name = rec.content
            elif rec.canonical_key == "user.name":
                identity.user_name = rec.content
                if not identity.user_display_name:
                    identity.user_display_name = rec.content
            elif rec.canonical_key == "agent.persona.relationship":
                identity.relationship_style = rec.content
            elif rec.canonical_key == "user.preference.response_style":
                identity.response_style = rec.content

        return identity

    # ------------------------------------------------------------------
    # Context Building
    # ------------------------------------------------------------------

    def build_context(self, agent_runtime_id: str = "") -> ReadContext:
        """Build context: exact keys for identity + Retriever for dynamic memories."""
        identity = self.resolve_identity(agent_runtime_id)

        # Policy: exact keys ONLY (no type-based fallback)
        policy_records = self._store.get_by_context_roles(["policy"])
        policies = [
            {"id": r.id, "content": r.content, "key": r.canonical_key,
             "importance": r.importance or 0.5}
            for r in policy_records
        ]

        # Dynamic memories: use MemoryRetriever (scope-ranked, NOT type-based)
        from aiive.memory.memory_retriever import MemoryRetriever, ScopeContext
        retriever = MemoryRetriever(self._store.db_session())
        user_memories = retriever.retrieve(ScopeContext())  # global default

        return ReadContext(
            runtime_identity=identity,
            policies=policies,
            user_memories=user_memories,
        )
