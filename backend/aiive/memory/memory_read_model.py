"""MemoryReadModel: deterministic key-based memory reader.

Provides Runtime Identity and Policy resolution using MemoryKeyRegistry
context roles. Does NOT perform full table scans — queries by specific
canonical_keys registered for each context role.

V2: dynamic, query-aware recall is handled by AutomaticRecallEngine
(see `automatic_recall.py`). The Kernel Contract keeps Runtime Identity +
Policy (exact keys) only.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence
from typing import Any

from aiive.db.models import MemoryRecord
from aiive.memory.memory_policy import MemoryPolicyEngine, MemoryReadChannel
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
        if self.user_name:
            result["user_name"] = self.user_name
        if self.relationship_style:
            result["relationship_style"] = self.relationship_style
        if self.response_style:
            result["response_style"] = self.response_style
        return result


class MemoryReadModel:
    """Deterministic memory reader using MemoryKeyRegistry context roles.

    Usage:
        model = MemoryReadModel(MemoryStore(db))
        identity = model.resolve_identity()
        policies = model.resolve_policies()
    """

    def __init__(self, store: MemoryStore) -> None:
        self._store: MemoryStore = store
        self._policy = MemoryPolicyEngine()

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
            content = self._policy.render_content(
                rec.content, rec.sensitivity, MemoryReadChannel.LLM_CONTEXT,
            )
            if content is None:
                continue
            if rec.canonical_key == "agent.display_name":
                identity.agent_display_name = content
            elif rec.canonical_key == "user.display_name":
                identity.user_display_name = content
            elif rec.canonical_key == "user.name":
                identity.user_name = content
                if not identity.user_display_name:
                    identity.user_display_name = content
            elif rec.canonical_key == "agent.persona.relationship":
                identity.relationship_style = content
            elif rec.canonical_key == "agent.persona.tone":
                identity.response_style = content
            elif rec.canonical_key == "user.preference.response_style":
                identity.response_style = content

        return identity

    # ------------------------------------------------------------------
    # Policy (exact keys only — part of the stable Kernel Contract)
    # ------------------------------------------------------------------

    def resolve_policies(self) -> list[dict[str, Any]]:
        """Resolve policy records via exact 'policy' context role keys."""
        policy_records = self._store.get_by_context_roles(["policy"])
        result: list[dict[str, Any]] = []
        for record in policy_records:
            content = self._policy.render_content(
                record.content, record.sensitivity, MemoryReadChannel.LLM_CONTEXT,
            )
            if content is not None:
                result.append({
                    "id": record.id,
                    "content": content,
                    "key": record.canonical_key,
                    "importance": record.importance or 0.5,
                })
        return result
