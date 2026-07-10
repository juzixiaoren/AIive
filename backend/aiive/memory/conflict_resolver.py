"""ConflictResolver: determines operation for a proposal against existing records.

Queries both active+valid AND candidate records for candidate promotion.
Uses conflict_policy from MemoryKeyRegistry (latest_value_wins for display_name, etc.).
Single key with conflict_policy=latest_value_wins → different content → supersede.
Single key with conflict_policy=revise_allowed → revises or uncertain.
Multi key deduplicates by content_hash.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from collections.abc import Sequence
from typing import Any

from aiive.db.models import MemoryRecord
from aiive.memory.memory_key_registry import MemoryKeyRegistry, get_memory_key_registry
from aiive.memory.memory_types import LifecycleState, MemoryProposal


@dataclass
class ResolutionResult:
    """Conflict resolution outcome."""
    operation: str  # create / reinforce / revise / supersede / promote / ignore
    existing_record: MemoryRecord | None = None
    candidate_record: MemoryRecord | None = None  # candidate for promotion
    existing_records: Sequence[MemoryRecord] = field(default_factory=list)
    reason: str = ""


class ConflictResolver:
    """Determines which lineage operation to apply."""

    def __init__(self, registry: MemoryKeyRegistry | None = None) -> None:
        self._registry: MemoryKeyRegistry = registry or get_memory_key_registry()

    def resolve(
        self,
        proposal: MemoryProposal,
        existing_records: Sequence[MemoryRecord],
    ) -> ResolutionResult:
        """Compare proposal against existing records (active+valid AND candidate).

        Args:
            proposal: Normalized MemoryProposal.
            existing_records: Records with same key+scope, including both
                              active+valid and candidate (from get_active_by_key_scope_locked).
        """
        key_spec = self._registry.resolve(proposal.canonical_key)
        cardinality: str = key_spec.cardinality if key_spec else "multi"
        conflict_policy: str = key_spec.conflict_policy if key_spec else "supersede"
        content_hash: str = proposal.content_hash or proposal.compute_content_hash()
        structured_hash: str = (
            self._hash_structured(proposal.structured_value)
            if proposal.structured_value else ""
        )

        # Separate active+valid from candidate
        active_valid = [r for r in existing_records if (
            r.lifecycle_state == LifecycleState.ACTIVE.value
            and (r.validity_state or "") in ("valid", "")
        )]
        candidates = [r for r in existing_records if (
            r.lifecycle_state == LifecycleState.CANDIDATE.value
        )]

        if not active_valid and not candidates:
            return ResolutionResult(operation="create", reason="No existing records")

        # --- Single cardinality ---
        if cardinality == "single":
            if active_valid:
                existing = active_valid[0]
                if self._hash_matches(existing, content_hash, structured_hash):
                    return ResolutionResult(
                        operation="reinforce",
                        existing_record=existing,
                        reason="Single key: same content, reinforcing",
                    )
                # Different content
                if conflict_policy == "latest_value_wins":
                    return ResolutionResult(
                        operation="supersede",
                        existing_record=existing,
                        reason="Single key (latest_value_wins): superseding",
                    )
                else:
                    # revise_allowed / merge: decide revise vs supersede based on change size
                    op = "revise" if self._is_minor_change(proposal, existing) else "supersede"
                    return ResolutionResult(
                        operation=op,
                        existing_record=existing,
                        reason=f"Single key: {op} existing",
                    )
            # No active+valid but has candidates → check for promotion
            if candidates:
                match = self._find_matching_candidate(candidates, content_hash, structured_hash)
                if match:
                    return ResolutionResult(
                        operation="promote",
                        candidate_record=match,
                        reason="Candidate matched: promoting to active",
                    )
            return ResolutionResult(operation="create", reason="No matching records")

        # --- Multi cardinality ---
        return self._resolve_multi(
            proposal, active_valid, candidates, content_hash, structured_hash
        )

    def _resolve_multi(
        self,
        _proposal: MemoryProposal,
        active_valid: list[MemoryRecord],
        candidates: list[MemoryRecord],
        content_hash: str,
        _structured_hash: str,
    ) -> ResolutionResult:
        # Dedup by hash on active
        for rec in active_valid:
            if self._hash_matches(rec, content_hash, _structured_hash):
                return ResolutionResult(
                    operation="reinforce",
                    existing_record=rec,
                    reason="Multi key: identical content, reinforcing",
                )
        # Check candidates for promotion
        for rec in candidates:
            if self._hash_matches(rec, content_hash, _structured_hash):
                return ResolutionResult(
                    operation="promote",
                    candidate_record=rec,
                    reason="Multi key: candidate matched, promoting",
                )
        # No match → create new
        return ResolutionResult(
            operation="create",
            reason="Multi key: creating new record",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_matches(record: MemoryRecord, content_hash: str, structured_hash: str) -> bool:
        rec_hash = (record.content_hash or
                    hashlib.sha256((record.content or "").encode()).hexdigest()[:16])
        if rec_hash == content_hash:
            return True
        if structured_hash and record.structured_value_hash:
            return record.structured_value_hash == structured_hash
        return False

    @staticmethod
    def _is_minor_change(proposal: MemoryProposal, existing: MemoryRecord) -> bool:
        """Minor change = same key, similar content (e.g., phrasing change)."""
        old = (existing.content or "").lower()
        new = proposal.content.lower()
        if abs(len(new) - len(old)) < max(len(old), 1) * 0.3:
            return True
        # Shared word ratio > 60% → minor
        old_words = set(old.split())
        new_words = set(new.split())
        if old_words and new_words:
            overlap = len(old_words & new_words) / max(len(old_words), len(new_words))
            return overlap > 0.6
        return False

    @staticmethod
    def _find_matching_candidate(
        candidates: list[MemoryRecord],
        content_hash: str,
        _structured_hash: str,
    ) -> MemoryRecord | None:
        for rec in candidates:
            rec_hash = (rec.content_hash or
                        hashlib.sha256((rec.content or "").encode()).hexdigest()[:16])
            if rec_hash == content_hash:
                return rec
        return None

    @staticmethod
    def _hash_structured(value: dict[str, Any] | None) -> str:
        if value is None:
            return ""
        try:
            import json
            raw = json.dumps(value, sort_keys=True, ensure_ascii=False)
            return hashlib.sha256(raw.encode()).hexdigest()[:16]
        except Exception:
            return ""
