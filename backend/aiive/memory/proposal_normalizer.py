"""ProposalNormalizer: standardizes incoming data into unified MemoryProposal.

Handles:
- Memory type normalization (legacy → canonical)
- Key resolution via MemoryKeyRegistry
- Scope inference
- Evidence validation
- Idempotency key computation

All sources (MemoryExtractor, tool calls, maintenance, steward enricher)
must output normalized MemoryProposal through this class.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aiive.memory.memory_key_registry import MemoryKeyRegistry, get_memory_key_registry
from aiive.memory.memory_types import (
    EvidenceItem,
    MemoryProposal,
    MemoryType,
    ScopeType,
    TrustLevel,
    LEGACY_TYPE_MAP,
    is_canonical_type,
    validate_scope,
)

logger = logging.getLogger(__name__)


@dataclass
class NormalizationResult:
    """Result of proposal normalization."""
    proposal: MemoryProposal | None = None
    error: str | None = None


class ProposalNormalizer:
    """Normalizes raw extraction/tool/maintenance data into canonical MemoryProposal."""

    def __init__(self, registry: MemoryKeyRegistry | None = None) -> None:
        self._registry: MemoryKeyRegistry = registry or get_memory_key_registry()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def normalize(
        self,
        content: str,
        memory_type_hint: str | None = None,
        memory_key_hint: str | None = None,
        confidence: float = 0.5,
        importance: float = 0.5,
        trust_level: str = TrustLevel.SEMI_TRUSTED.value,
        evidence: list[dict[str, Any]] | None = None,
        source_event_ids: list[str] | None = None,
        assistant_event_ids: list[str] | None = None,
        structured_value: dict[str, Any] | None = None,
        keywords: list[str] | None = None,
        extractor_name: str = "",
        extractor_version: str = "",
        proposed_operation: str = "create",
        thread_id: str = "",
    ) -> NormalizationResult:
        """Normalize raw inputs into a canonical MemoryProposal.

        Args:
            content: Memory content text.
            memory_type_hint: Legacy or canonical type string from extractor.
            memory_key_hint: Raw key from extractor (may need normalization).
            confidence: 0.0–1.0.
            importance: 0.0–1.0.
            trust_level: Evidence trust level.
            evidence: Raw evidence items.
            source_event_ids: Source event IDs.
            assistant_event_ids: 属于 assistant 回复事件的 Event.id 列表，
                其中的事件会被标记为 llm_reply，其余保持 user_message。
            structured_value: Optional structured value.
            extractor_name: Name of the extractor.
            extractor_version: Version of the extractor.
            proposed_operation: Extractor's suggested operation.
            thread_id: Current thread ID for scope inference.

        Returns:
            NormalizationResult with proposal or error.
        """
        # 1. Map memory type to canonical
        canonical_type: str | None = self._resolve_type(memory_type_hint)
        if canonical_type is None:
            return NormalizationResult(
                error=f"Unknown memory_type '{memory_type_hint}': not a canonical type"
            )
        if not is_canonical_type(canonical_type):
            return NormalizationResult(
                error=f"Unknown memory_type '{memory_type_hint}': not a canonical type"
            )

        # 2. Resolve canonical key via registry
        canonical_key: str | None = self._resolve_key(
            memory_key_hint, canonical_type, content
        )
        if canonical_key is None:
            return NormalizationResult(
                error="Could not resolve canonical_key from provided hints"
            )

        # 3. Get key spec for cardinality + scope
        key_spec = self._registry.resolve(canonical_key)
        default_scope: str = key_spec.default_scope if key_spec else ScopeType.GLOBAL.value

        # 4. Infer scope
        scope_type, scope_id = self._infer_scope(
            default_scope, canonical_type, canonical_key, thread_id
        )

        # 5. Build evidence items
        evidence_items: list[EvidenceItem] = []
        if evidence:
            for e in evidence:
                evidence_items.append(EvidenceItem(
                    source_event_id=e.get("source_event_id"),
                    source_type=e.get("source_type", "user_message"),
                    trust_level=e.get("trust_level", trust_level),
                    relation=e.get("relation", "supports"),
                    content_span=e.get("content_span"),
                ))
        elif source_event_ids:
            assistant_set = set(assistant_event_ids or [])
            for seid in source_event_ids:
                # 用户消息事件保留 trusted 的 user_message；assistant 回复事件
                # 标记为 llm_reply（外部/不可信来源），使 provenance 语义精确。
                # llm_reply 证据 relation=derived_from：仅作 provenance，
                # 不参与 Gate 的写入权威判定。
                is_assistant = seid in assistant_set
                evidence_items.append(EvidenceItem(
                    source_event_id=seid,
                    source_type="llm_reply" if is_assistant else "user_message",
                    trust_level=trust_level,
                    relation="derived_from" if is_assistant else "supports",
                ))

        # 6. Build proposal
        proposal = MemoryProposal(
            source_event_ids=source_event_ids or [],
            memory_type=canonical_type,
            canonical_key=canonical_key,
            scope_type=scope_type,
            scope_id=scope_id,
            content=content,
            structured_value=structured_value,
            keywords=keywords or [],
            evidence=evidence_items,
            trust_level=trust_level,
            confidence=confidence,
            importance=importance,
            proposed_operation=proposed_operation,
            extractor_name=extractor_name,
            extractor_version=extractor_version,
        )
        proposal.compute_request_idempotency()

        return NormalizationResult(proposal=proposal)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_type(self, type_hint: str | None) -> str | None:
        """Resolve a type string to canonical MemoryType."""
        if not type_hint:
            return MemoryType.KNOWLEDGE.value
        if is_canonical_type(type_hint):
            return type_hint
        mapped = LEGACY_TYPE_MAP.get(type_hint)
        if mapped is not None:
            return mapped
        return None

    def _resolve_key(
        self,
        key_hint: str | None,
        canonical_type: str,
        content: str,
    ) -> str | None:
        """Resolve canonical key from hint, type, and content."""
        if key_hint and key_hint.strip():
            return key_hint.strip()

        # Fallback: derive from type + content
        if canonical_type == MemoryType.USER_PROFILE.value:
            slug = content.lower().replace(" ", "_")[:40]
            return f"user.preference.{slug}"
        if canonical_type == MemoryType.KNOWLEDGE.value:
            slug = content.lower().replace(" ", "_")[:40]
            return f"knowledge.{slug}"
        if canonical_type == MemoryType.EPISODIC.value:
            slug = content.lower().replace(" ", "_")[:40]
            return f"episodic.{slug}"
        if canonical_type == MemoryType.AGENT_SELF.value:
            return "agent.display_name"

        return None

    @staticmethod
    def _infer_scope(
        default_scope: str,
        _canonical_type: str,
        canonical_key: str,
        _thread_id: str = "",
    ) -> tuple[str, str | None]:
        """Infer scope_type and scope_id."""
        scope_type = default_scope
        scope_id: str | None = None

        # project scope with key prefix
        if canonical_key.startswith("project."):
            scope_type = ScopeType.PROJECT.value
            # Extract project name from key: project.<name>.<topic>
            parts = canonical_key.split(".")
            if len(parts) >= 2:
                scope_id = parts[1]

        if scope_type == ScopeType.GLOBAL.value:
            scope_id = None

        try:
            validate_scope(scope_type, scope_id)
        except ValueError:
            logger.warning(
                "Scope validation failed: type=%s id=%s, falling back to global",
                scope_type, scope_id,
            )
            scope_type = ScopeType.GLOBAL.value
            scope_id = None

        return scope_type, scope_id
