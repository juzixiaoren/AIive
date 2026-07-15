"""Recall models for the V2 memory read architecture.

Defines the data contracts used by Automatic Recall, Agent-Initiated Recall,
Core Memory projection, and Context Assembly.

These are pure Pydantic models — no DB access, no magic numbers
(budgets live in `recall_config.RecallConfig`).
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class ScopeContext(BaseModel):
    """Ordered scope chain for retrieval.

    Runtime provides this. The model may narrow the filter range but must
    never expand beyond authorized scopes.

    Priority (highest → lowest):
        thread > project > workspace > capability > environment > global
    """

    thread_id: str | None = None
    project_id: str | None = None
    workspace_id: str | None = None
    capability_ids: list[str] = Field(default_factory=list)
    environment_id: str | None = None

    def chain(self) -> list[tuple[str, str | None]]:
        """Return ordered (scope_type, scope_id) tuples to query."""
        from aiive.memory.memory_types import ScopeType

        result: list[tuple[str, str | None]] = []
        if self.thread_id:
            result.append((ScopeType.THREAD.value, self.thread_id))
        if self.project_id:
            result.append((ScopeType.PROJECT.value, self.project_id))
        if self.workspace_id:
            result.append((ScopeType.WORKSPACE.value, self.workspace_id))
        if self.capability_ids:
            for cap in self.capability_ids:
                result.append((ScopeType.CAPABILITY.value, cap))
        if self.environment_id:
            result.append((ScopeType.ENVIRONMENT.value, self.environment_id))
        result.append((ScopeType.GLOBAL.value, None))
        return result


class MemoryRecallRequest(BaseModel):
    """A single Automatic / Agent-Initiated recall request."""

    query: str
    active_goal: str | None = None
    thread_summary: str | None = None
    scope_context: ScopeContext
    top_k: int = 8
    token_budget: int = 1200
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])


class MemoryRecallItem(BaseModel):
    """A single recalled memory with its multi-signal scores."""

    memory_id: str
    content: str
    memory_type: str
    canonical_key: str
    scope_type: str
    scope_id: str | None = None
    relevance_score: float = 0.0
    scope_score: float = 0.0
    recency_score: float = 0.0
    importance_score: float = 0.0
    trust_level: str = "semi_trusted"
    authority: str = ""
    validity_state: str = "valid"
    source_refs: list[str] = Field(default_factory=list)
    record_version: int = 1
    token_cost: int = 0
    route: str = ""          # which route produced this candidate (exact/fts/episode/vector/graph)
    fused_score: float = 0.0  # final fused score after fusion


class MemoryRecallPack(BaseModel):
    """The fused, token-packed result of a recall request."""

    request_id: str
    items: list[MemoryRecallItem] = Field(default_factory=list)
    token_count: int = 0
    excluded_count: int = 0


class RecallCandidateTrace(BaseModel):
    """Per-candidate trace for explainability (recall_runs / Inspector)."""

    memory_id: str
    route: str
    raw_score: float = 0.0
    fused_score: float = 0.0
    selected: bool = False
    exclusion_reason: str = ""
    token_cost: int = 0


class CoreMemoryBlock(BaseModel):
    """A single small, stable Core Memory projection block."""

    block_name: str            # core.human_identity / core.interaction_defaults / core.agent_persona
    content: str
    source_memory_ids: list[str] = Field(default_factory=list)
    projection_version: int = 1
    token_count: int = 0

    @classmethod
    def estimate_tokens(cls, text: str) -> int:
        return max(1, len(text) // 4)



