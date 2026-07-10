"""MemoryMaintenance: periodic memory lifecycle management.

Generates maintenance proposals (sleep/archive/consolidate/stale) for
MemoryWriteService. Does NOT directly modify MemoryRecords.

Maintenance constraints:
- Can only generate sleep, archive, consolidate, stale, merge proposals.
- Must NOT create, revise, or supersede user_profile or policy records
  without trusted user evidence.
- Maintenance proposals go through the same ProposalNormalizer + Gate pipeline.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import MemoryRecord
from aiive.memory.memory_types import (
    EvidenceItem,
    LifecycleState,
    MemoryProposal,
    MemoryType,
    TrustLevel,
)

logger = logging.getLogger(__name__)


# Types that maintenance MUST NOT create/revise/supersede
_PROTECTED_TYPES: frozenset[str] = frozenset({
    MemoryType.USER_PROFILE.value,
    MemoryType.POLICY.value,
    MemoryType.AGENT_SELF.value,
})


class MemoryMaintenance:
    """Memory lifecycle maintenance — generates proposals only.

    Does NOT directly modify MemoryRecords. All state changes go through
    MemoryWriteService via maintenance proposals.
    """

    def __init__(self, db: Session) -> None:
        self._db: Session = db

    # ------------------------------------------------------------------
    # Scan — identify maintenance candidates
    # ------------------------------------------------------------------

    def scan(self) -> dict[str, Any]:
        """Scan for memory records needing maintenance.

        Returns candidates for sleep, archive, consolidate, and stale detection.
        """
        records = (
            self._db.query(MemoryRecord)
            .filter(MemoryRecord.lifecycle_state.in_([
                LifecycleState.ACTIVE.value,
                LifecycleState.CANDIDATE.value,
                LifecycleState.SLEEPING.value,
            ]))
            .all()
        )

        sleep_candidates: list[str] = []
        archive_candidates: list[str] = []
        stale_candidates: list[str] = []
        consolidate_candidates: list[dict[str, Any]] = []

        now = datetime.now(timezone.utc)

        for r in records:
            if r.pinned:
                continue

            age_days = (now - (r.observed_at or r.created_at).replace(
                tzinfo=timezone.utc
            )).days if (r.observed_at or r.created_at) else 0

            # Stale candidate: not observed for 7+ days (but was active)
            if r.lifecycle_state == LifecycleState.ACTIVE.value and age_days > 7:
                if r.memory_type in _PROTECTED_TYPES:
                    # Protected types: sleep instead of archive
                    sleep_candidates.append(r.id)
                else:
                    stale_candidates.append(r.id)

            # Expired candidate: candidate for 7+ days → archive
            if r.lifecycle_state == LifecycleState.CANDIDATE.value and age_days > 7:
                archive_candidates.append(r.id)

            # Archived candidate: sleeping for 30+ days → archive
            if r.lifecycle_state == LifecycleState.SLEEPING.value and age_days > 30:
                archive_candidates.append(r.id)

        return {
            "total_scanned": len(records),
            "sleep_candidates": len(sleep_candidates),
            "archive_candidates": len(archive_candidates),
            "stale_candidates": len(stale_candidates),
            "consolidate_candidates": len(consolidate_candidates),
            "sleep_ids": sleep_candidates,
            "archive_ids": archive_candidates,
            "stale_ids": stale_candidates,
            "consolidate_ids": consolidate_candidates,
        }

    # ------------------------------------------------------------------
    # Generate maintenance proposals
    # ------------------------------------------------------------------

    def generate_sleep_proposals(self, memory_ids: list[str]) -> list[MemoryProposal]:
        """Generate sleep proposals for specified records.

        Protected types (user_profile, policy, agent_self) are filtered out.
        """
        proposals: list[MemoryProposal] = []
        for mid in memory_ids:
            record = self._db.get(MemoryRecord, mid)
            if record is None:
                continue
            if record.memory_type in _PROTECTED_TYPES:
                logger.info("Skipping sleep for protected type: %s (%s)", mid, record.memory_type)
                continue

            proposal = MemoryProposal(
                source_event_ids=[mid],
                memory_type=record.memory_type,
                canonical_key=record.canonical_key or "",
                scope_type=record.scope_type or "global",
                scope_id=record.scope_id,
                content=record.content or "",
                proposed_operation="sleep",
                extractor_name="MemoryMaintenance",
                extractor_version="1.0",
                evidence=[EvidenceItem(
                    source_type="maintenance",
                    trust_level=TrustLevel.SEMI_TRUSTED.value,
                    relation="supports",
                )],
            )
            proposal.compute_request_idempotency()
            proposals.append(proposal)
        return proposals

    def generate_archive_proposals(self, memory_ids: list[str]) -> list[MemoryProposal]:
        """Generate archive proposals for specified records."""
        proposals: list[MemoryProposal] = []
        for mid in memory_ids:
            record = self._db.get(MemoryRecord, mid)
            if record is None:
                continue

            proposal = MemoryProposal(
                source_event_ids=[mid],
                memory_type=record.memory_type,
                canonical_key=record.canonical_key or "",
                scope_type=record.scope_type or "global",
                scope_id=record.scope_id,
                content=record.content or "",
                proposed_operation="archive",
                extractor_name="MemoryMaintenance",
                extractor_version="1.0",
                evidence=[EvidenceItem(
                    source_type="maintenance",
                    trust_level=TrustLevel.SEMI_TRUSTED.value,
                    relation="supports",
                )],
            )
            proposal.compute_request_idempotency()
            proposals.append(proposal)
        return proposals

    def generate_wake_proposals(self, memory_ids: list[str]) -> list[MemoryProposal]:
        """Generate wake proposals for sleeping records."""
        proposals: list[MemoryProposal] = []
        for mid in memory_ids:
            record = self._db.get(MemoryRecord, mid)
            if record is None:
                continue
            if record.lifecycle_state != LifecycleState.SLEEPING.value:
                continue

            proposal = MemoryProposal(
                source_event_ids=[mid],
                memory_type=record.memory_type,
                canonical_key=record.canonical_key or "",
                scope_type=record.scope_type or "global",
                scope_id=record.scope_id,
                content=record.content or "",
                proposed_operation="wake",
                extractor_name="MemoryMaintenance",
                extractor_version="1.0",
                evidence=[EvidenceItem(
                    source_type="maintenance",
                    trust_level=TrustLevel.SEMI_TRUSTED.value,
                    relation="supports",
                )],
            )
            proposal.compute_request_idempotency()
            proposals.append(proposal)
        return proposals

    # ------------------------------------------------------------------
    # Backward-compatible direct operations (deprecated)
    # These now generate proposals and should run through MemoryWriteService.
    # ------------------------------------------------------------------

    def forget(self, _memory_id: str = "", _reason: str = "") -> dict[str, Any]:
        """DEPRECATED: Use MemoryWriteService.forget() instead.

        Kept for backward compatibility in builtin_tools.
        The builtin handler should migrate to MemoryWriteService.forget().
        """
        logger.warning(
            "MemoryMaintenance.forget() is deprecated. Use MemoryWriteService.forget()."
        )
        # Return structure that callers expect; actual forget must go through WriteService
        return {"ok": False, "error": "Deprecated: use MemoryWriteService.forget()"}

    def sleep(self, _memory_id: str = "") -> dict[str, Any]:
        """DEPRECATED: Use MemoryWriteService.execute_maintenance() instead."""
        logger.warning(
            "MemoryMaintenance.sleep() is deprecated. Use MemoryWriteService.execute_maintenance()."
        )
        return {"ok": False, "error": "Deprecated: use MemoryWriteService.execute_maintenance()"}

    def archive(self, _memory_id: str = "") -> dict[str, Any]:
        """DEPRECATED: Use MemoryWriteService.execute_maintenance() instead."""
        logger.warning(
            "MemoryMaintenance.archive() is deprecated. Use MemoryWriteService.execute_maintenance()."
        )
        return {"ok": False, "error": "Deprecated: use MemoryWriteService.execute_maintenance()"}
