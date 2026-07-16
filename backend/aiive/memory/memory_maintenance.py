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
from aiive.memory.memory_store import MemoryStore
from aiive.memory.maintenance_hashes import effective_last_accessed_at
from aiive.memory.recall_config import MaintenanceConfig
from aiive.memory.memory_types import (
    LifecycleState,
)

logger = logging.getLogger(__name__)


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
        """扫描需要维护的记忆记录，返回各类候选计数（只读诊断）。

        阈值与 planner（memory_maintenance_planner.plan_batch）保持一致，均取自
        MaintenanceConfig，避免诊断数字与真实决策口径不一致。
        """
        cfg = MaintenanceConfig()
        store = MemoryStore(self._db)
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
            # effective_pinned：pinned 或 retention_policy=='pinned' 禁止任何自动降级
            if r.pinned or r.retention_policy == "pinned":
                continue

            # expired ephemeral（与 planner 一致）
            if (
                r.retention_policy == "ephemeral"
                and r.valid_to is not None
                and r.valid_to <= now
                and r.lifecycle_state in (
                    LifecycleState.ACTIVE.value, LifecycleState.CANDIDATE.value,
                )
            ):
                archive_candidates.append(r.id)
                continue

            # candidate：超过 candidate_ttl_days 无新证据则过期归档（与 planner 一致）
            if r.lifecycle_state == LifecycleState.CANDIDATE.value:
                if r.created_at and (now - r.created_at).days >= cfg.candidate_ttl_days:
                    archive_candidates.append(r.id)
                continue

            # active：importance 低于阈值且达到冷却期 → 冷却到 sleeping（与 planner 一致）
            if r.lifecycle_state == LifecycleState.ACTIVE.value:
                # user-required 受保护记录使用更长的冷却期（与 planner 一致）
                cooling_days = (
                    cfg.user_required_sleep_cooling_days
                    if store.is_user_required_protected(r.id)[0]
                    else cfg.sleep_cooling_days
                )
                eff = effective_last_accessed_at(
                    r.last_accessed_at, r.observed_at, r.created_at,
                )
                if (r.importance < cfg.importance_sleep_threshold
                        and (now - eff).days >= cooling_days):
                    sleep_candidates.append(r.id)
                continue

            # sleeping：planner 当前不自动归档 sleeping，故诊断中不计入 archive。
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
