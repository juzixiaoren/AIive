"""MemoryStore: internal memory persistence repository.

Provides structured CRUD for MemoryRecord, used exclusively by MemoryWriteService
and read-side components (MemoryReadModel).

Production write paths MUST NOT call create_record() directly; they must go through
MemoryWriteService.

Read paths (get_active, get_by_id, get_by_key_scope, etc.) are safe for
MemoryReadModel to use.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from collections.abc import Sequence
from typing import Any

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from aiive.db.models import (
    Event,
    MemoryEvidence,
    MemoryLineage,
    MemoryProposal as MemoryProposalModel,
    MemoryRecord,
)
from aiive.memory.memory_types import MemoryProposal, LifecycleState, ValidityState

logger = logging.getLogger(__name__)

# Phase 4 维护四条 lane（第 1 点）
_MAINTENANCE_LANES: frozenset[str] = frozenset({
    "changed",
    "expired_ephemeral",
    "candidate_due",
    "sleep_due",
})


class MemoryStore:
    """Internal repository for memory_records operations.

    Write operations: called only by MemoryWriteService.
    Read operations: called by MemoryReadModel, Context Builder.
    """

    def __init__(self, db: Session) -> None:
        self._db: Session = db

    # ------------------------------------------------------------------
    # Write operations (internal — called only by MemoryWriteService)
    # ------------------------------------------------------------------

    def create_record(
        self,
        proposal: MemoryProposal,
        lifecycle_state: str = LifecycleState.CANDIDATE.value,
        validity_state: str = ValidityState.VALID.value,
        supersedes: str | None = None,
        revision_of: str | None = None,
        revision_num: int = 1,
    ) -> MemoryRecord:
        """Create a new MemoryRecord from a normalized proposal.

        Called ONLY by MemoryWriteService.
        """
        now = datetime.now(timezone.utc)
        # 语义去重 hash 落库：content_hash 与 ConflictResolver._hash_matches 的
        # 回退算法一致（sha256[:16]）；structured_value_hash 与
        # ConflictResolver._hash_structured 一致。不落库会导致 maintenance
        # planner 的 merge_exact_duplicate 桶永不触发、resolver 的
        # structured_value_hash 分支不可达。
        from aiive.memory.conflict_resolver import ConflictResolver
        content_hash = proposal.content_hash or proposal.compute_content_hash()
        structured_value_hash = (
            ConflictResolver._hash_structured(proposal.structured_value)
            if proposal.structured_value else None
        ) or None
        record = MemoryRecord(
            id=str(uuid.uuid4()),
            memory_type=proposal.memory_type,
            canonical_key=proposal.canonical_key,
            cardinality=self._infer_cardinality(proposal.canonical_key),
            scope_type=proposal.scope_type,
            scope_id=proposal.scope_id,
            content=proposal.content,
            structured_value=proposal.structured_value,
            content_hash=content_hash,
            structured_value_hash=structured_value_hash,
            keywords=proposal.keywords or None,
            lifecycle_state=lifecycle_state,
            validity_state=validity_state,
            trust_level=proposal.trust_level,
            stability=proposal.stability,
            sensitivity=proposal.sensitivity,
            stability_score=proposal.stability_score,
            confidence=proposal.confidence,
            importance=proposal.importance,
            record_version=1,
            reinforce_count=0,
            source_event_id=proposal.source_event_ids[0] if proposal.source_event_ids else None,
            created_from=proposal.proposal_id,
            revision_of=revision_of,
            supersedes=supersedes,
            revision_num=revision_num,
            valid_from=now,
            valid_to=proposal.valid_to,
            retention_policy=proposal.retention_policy,
            observed_at=now,
            created_at=now,
            updated_at=now,
        )
        self._db.add(record)
        self._db.flush()
        return record

    # ------------------------------------------------------------------
    # Read operations — safe for any consumer
    # ------------------------------------------------------------------

    def get_by_id(self, memory_id: str) -> MemoryRecord | None:
        """Get a single record by ID."""
        return self._db.get(MemoryRecord, memory_id)

    # Active = lifecycle='active' AND validity='valid'
    _ACTIVE_VALID: tuple[Any, Any] = (
        MemoryRecord.lifecycle_state == LifecycleState.ACTIVE.value,
        MemoryRecord.validity_state == ValidityState.VALID.value,
    )

    def get_active_by_key_scope_locked(
        self, canonical_key: str, scope_type: str, scope_id: str | None
    ) -> Sequence[MemoryRecord]:
        """SELECT FOR UPDATE on active+valid records + candidates (for conflict resolution).

        Returns all records with same key+scope that are either active+valid or candidate.
        This enables candidate promotion in ConflictResolver.
        """
        query = self._db.query(MemoryRecord).filter(
            MemoryRecord.canonical_key == canonical_key,
            MemoryRecord.scope_type == scope_type,
            MemoryRecord.lifecycle_state.in_([
                LifecycleState.ACTIVE.value,
                LifecycleState.CANDIDATE.value,
            ]),
        )
        if scope_id is not None:
            query = query.filter(MemoryRecord.scope_id == scope_id)
        else:
            query = query.filter(MemoryRecord.scope_id.is_(None))
        # active 排在 candidate 之前（显式 CASE；字符串 desc 会把 candidate 排前）
        state_order = case(
            (MemoryRecord.lifecycle_state == LifecycleState.ACTIVE.value, 0),
            else_=1,
        )
        try:
            return query.order_by(
                state_order.asc(),  # active before candidate
                MemoryRecord.updated_at.desc(),
            ).with_for_update().all()
        except Exception:
            logger.warning("记忆查询加锁失败，回退非锁定查询（并发风险）", exc_info=True)
            return query.order_by(
                state_order.asc(),
                MemoryRecord.updated_at.desc(),
            ).all()

    def get_sleeping_by_key_scope_locked(
        self, canonical_key: str, scope_type: str, scope_id: str | None
    ) -> Sequence[MemoryRecord]:
        """SELECT FOR UPDATE on sleeping records by key+scope.

        供 write 路径在 resolve 前置阶段唤醒 sleeping 同键记忆（J.4/J.5 wake 闭合）。
        按 `updated_at` 倒序返回，调用方取 [0] 作为最近更新的唤醒目标。
        """
        query = self._db.query(MemoryRecord).filter(
            MemoryRecord.canonical_key == canonical_key,
            MemoryRecord.scope_type == scope_type,
            MemoryRecord.lifecycle_state == LifecycleState.SLEEPING.value,
        )
        if scope_id is not None:
            query = query.filter(MemoryRecord.scope_id == scope_id)
        else:
            query = query.filter(MemoryRecord.scope_id.is_(None))
        try:
            return query.order_by(MemoryRecord.updated_at.desc()).with_for_update().all()
        except Exception:
            logger.warning("睡眠记忆查询加锁失败，回退非锁定查询（并发风险）", exc_info=True)
            return query.order_by(MemoryRecord.updated_at.desc()).all()

    def get_by_context_roles(
        self, roles: Sequence[str]
    ) -> Sequence[MemoryRecord]:
        """Get active+valid records by context role keys (exact + pattern prefixes).

        Query-based: matches exact canonical_keys OR canonical_key LIKE prefix%.
        No full table scan.
        """
        from sqlalchemy import or_

        from aiive.memory.memory_key_registry import get_memory_key_registry

        registry = get_memory_key_registry()
        exact_keys: set[str] = set()
        prefixes: list[str] = []
        for role in roles:
            exact_keys.update(registry.get_context_role_keys(role))
            prefixes.extend(registry.get_context_role_patterns(role))
        if not exact_keys and not prefixes:
            return []
        conditions = []
        if exact_keys:
            conditions.append(MemoryRecord.canonical_key.in_(list(exact_keys)))
        for pfx in prefixes:
            conditions.append(MemoryRecord.canonical_key.like(f"{pfx}%"))
        return (
            self._db.query(MemoryRecord)
            .filter(or_(*conditions), *self._ACTIVE_VALID)
            .order_by(MemoryRecord.updated_at.desc())
            .all()
        )

    def list_all(self, limit: int = 100) -> Sequence[MemoryRecord]:
        """List all records (for inspection)."""
        return (
            self._db.query(MemoryRecord)
            .order_by(MemoryRecord.updated_at.desc())
            .limit(limit)
            .all()
        )

    # ------------------------------------------------------------------
    # Phase 4: 维护 dirty-set lane 查询与 Batch 选择
    # ------------------------------------------------------------------

    def select_maintenance_seeds(
        self,
        lane: str,
        *,
        cutoff_updated_at: datetime,
        cursor_time: datetime | None,
        cursor_id: str | None,
        limit: int,
        now: datetime,
        ttl_days: int = 30,
        cooling_days: int | None = None,
    ) -> list[MemoryRecord]:
        """选取某 lane 的 seed 记忆（升序，最多 `limit` 条）。

        各 lane 以自身时间维为游标（第 1 点）：
        - changed → updated_at（受 cutoff_updated_at 上界约束）
        - expired_ephemeral → valid_to（不受 updated_at 旧 high-water 限制）
        - candidate_due → created_at（派生 deadline，不受 updated_at 限制）
        - sleep_due → effective_last_accessed_at（不受 updated_at 限制）
        `cursor_time`/`cursor_id` 为续跑游标（不含已处理记录）。
        """
        if lane not in _MAINTENANCE_LANES:
            raise ValueError(f"未知维护 lane: {lane}")

        # 时区归一化：SQLite 返回 naive、Postgres 返回 aware，统一按 naive UTC 比较。
        def _naive(dt: datetime | None) -> Any:
            return dt.replace(tzinfo=None) if dt and dt.tzinfo else dt

        cutoff = _naive(cutoff_updated_at)
        now_n = _naive(now)
        ctime = _naive(cursor_time)

        MR = MemoryRecord
        q = self._db.query(MR)

        if lane == "changed":
            q = q.filter(
                MR.lifecycle_state.in_([
                    LifecycleState.CANDIDATE.value,
                    LifecycleState.ACTIVE.value,
                    LifecycleState.SLEEPING.value,
                ]),
                MR.updated_at <= cutoff,
            )
            if ctime is not None:
                q = q.filter(
                    or_(
                        MR.updated_at > ctime,
                        and_(MR.updated_at == ctime, MR.id > cursor_id),
                    )
                )
            q = q.order_by(MR.updated_at.asc(), MR.id.asc())

        elif lane == "expired_ephemeral":
            q = q.filter(
                MR.lifecycle_state.in_([
                    LifecycleState.ACTIVE.value,
                    LifecycleState.CANDIDATE.value,
                ]),
                MR.retention_policy == "ephemeral",
                MR.valid_to.isnot(None),
                MR.valid_to <= now_n,
            )
            if ctime is not None:
                q = q.filter(
                    or_(
                        MR.valid_to > ctime,
                        and_(MR.valid_to == ctime, MR.id > cursor_id),
                    )
                )
            q = q.order_by(MR.valid_to.asc(), MR.id.asc())

        elif lane == "candidate_due":
            deadline = now_n - timedelta(days=ttl_days)
            q = q.filter(
                MR.lifecycle_state == LifecycleState.CANDIDATE.value,
                MR.created_at <= deadline,
            )
            if ctime is not None:
                q = q.filter(
                    or_(
                        MR.created_at > ctime,
                        and_(MR.created_at == ctime, MR.id > cursor_id),
                    )
                )
            q = q.order_by(MR.created_at.asc(), MR.id.asc())

        elif lane == "sleep_due":
            # 有效访问时间回退：last_accessed_at → observed_at → created_at
            eff = func.coalesce(MR.last_accessed_at, MR.observed_at, MR.created_at)
            q = q.filter(
                MR.lifecycle_state.in_([
                    LifecycleState.ACTIVE.value,
                    LifecycleState.SLEEPING.value,
                ]),
                MR.pinned == False,  # noqa: E712
                MR.retention_policy != "pinned",
            )
            # 冷却阈值下推：未达冷却期的不选为 seed，避免每次 daily Run 把全部
            # 非 pinned 记录拉进 planner（planner 再用 cooling_days 过滤多数 no_op）。
            if cooling_days is not None:
                q = q.filter(eff <= now_n - timedelta(days=cooling_days))
            if ctime is not None:
                q = q.filter(
                    or_(
                        eff > ctime,
                        and_(eff == ctime, MR.id > cursor_id),
                    )
                )
            q = q.order_by(eff.asc(), MR.id.asc())

        return q.limit(limit).all()

    def expand_maintenance_neighbors(self, seed: MemoryRecord) -> list[MemoryRecord]:
        """扩展 seed 的同 (canonical_key, scope_type, scope_id) 邻域（第 4 点）。"""
        MR = MemoryRecord
        conditions = [
            MR.canonical_key == seed.canonical_key,
            MR.scope_type == seed.scope_type,
            MR.lifecycle_state.in_([
                LifecycleState.CANDIDATE.value,
                LifecycleState.ACTIVE.value,
                LifecycleState.SLEEPING.value,
            ]),
        ]
        if seed.scope_id is None:
            conditions.append(MR.scope_id.is_(None))
        else:
            conditions.append(MR.scope_id == seed.scope_id)
        return (
            self._db.query(MR)
            .filter(*conditions)
            .order_by(MR.id.asc())
            .all()
        )

    def count_independent_evidence(self, memory_id: str) -> int:
        """独立 evidence 计数：COUNT(DISTINCT source_event_id) 且 source_event 属真实 user_message Event。

        v1 不新增 `source_turn_record_id`（第 8 点）。
        """
        return (
            self._db.query(
                func.count(func.distinct(MemoryEvidence.source_event_id))
            )
            .select_from(MemoryEvidence)
            .join(Event, Event.id == MemoryEvidence.source_event_id)
            .filter(
                MemoryEvidence.memory_id == memory_id,
                Event.event_type == "user_message",
            )
            .scalar()
            or 0
        )

    def is_user_required_protected(self, memory_id: str) -> tuple[bool, list[str]]:
        """判定记忆是否受 user-required 保护，并汇总来源 proposal id（J.3）。

        沿三类来源追溯：直接 MemoryProposal / predecessor|successor lineage /
        merge·supersede 来源记录。命中任一即视为受保护。
        """
        source_ids: list[str] = []
        direct = (
            self._db.query(MemoryProposalModel)
            .filter(
                MemoryProposalModel.final_memory_id == memory_id,
                MemoryProposalModel.execution_mode == "user_required",
            )
            .first()
        )
        if direct is not None:
            source_ids.append(direct.proposal_id)

        lineages = (
            self._db.query(MemoryLineage)
            .filter(
                or_(
                    MemoryLineage.predecessor_id == memory_id,
                    MemoryLineage.successor_id == memory_id,
                )
            )
            .all()
        )
        for lin in lineages:
            other = (
                lin.successor_id
                if lin.predecessor_id == memory_id
                else lin.predecessor_id
            )
            if other == memory_id:
                continue
            via = (
                self._db.query(MemoryProposalModel)
                .filter(
                    MemoryProposalModel.final_memory_id == other,
                    MemoryProposalModel.execution_mode == "user_required",
                )
                .first()
            )
            if via is not None:
                source_ids.append(via.proposal_id)

        return (len(source_ids) > 0, sorted(set(source_ids)))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_cardinality(canonical_key: str) -> str:
        """Quick cardinality check without full registry resolution."""
        from aiive.memory.memory_key_registry import get_memory_key_registry
        registry = get_memory_key_registry()
        if registry.is_single_cardinality(canonical_key):
            return "single"
        return "multi"
