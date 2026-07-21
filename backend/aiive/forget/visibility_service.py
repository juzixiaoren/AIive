"""统一 fail-closed ForgetVisibilityService。

所有读取路径统一调用本服务，禁止各模块自行实现 forgotten 判断。
读取顺序：先查 Shield（选择器级），再查 Tombstone（实体级）。
异常时 fail-closed（默认不可见），不是 fail-open。

审计可见性规则：allow_raw_history && tombstone.allow_audit_read && !tombstone.content_purged
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from aiive.db.forget_models import ForgetShield, ForgetTombstone


class ForgetVisibilityService:
    """统一 forget 可见性服务。

    单例，无状态，不持有长事务。调用方传 Session 实例。
    """

    # ── Shield 检查 ──

    @staticmethod
    def is_shielded(
        session: Session,
        *,
        thread_id: str | None = None,
        event_id: str | None = None,
        turn_id: str | None = None,
        memory_id: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        target_time: datetime | None = None,
        now: datetime | None = None,
    ) -> bool:
        """检查目标是否被任意 active Shield 覆盖。

        命中规则（fail-closed）：
          - 实体级：target_type/target_id 直接匹配；
          - 选择器级：thread / scope / all_user_data 覆盖；
          - time_range：仅当提供了目标时间且落在 [time_from, time_to] 内才命中；
          - cutoff：仅覆盖创建时间早于 cutoff_created_at 的数据（target_time 提供时生效）。
        """
        _now = now or datetime.now(timezone.utc)
        q = session.query(ForgetShield).filter(
            ForgetShield.status == "active",
        )

        # 实体级直接命中
        entity_conditions: list[Any] = []
        if event_id:
            entity_conditions.append(
                (ForgetShield.target_type == "event")
                & (ForgetShield.target_id == event_id)
            )
        if turn_id:
            entity_conditions.append(
                (ForgetShield.target_type == "turn_record")
                & (ForgetShield.target_id == turn_id)
            )
        if memory_id:
            entity_conditions.append(
                (ForgetShield.target_type == "memory_record")
                & (ForgetShield.target_id == memory_id)
            )

        # 选择器级命中（含 all_user_data 全局覆盖）
        selector_conditions: list[Any] = [
            ForgetShield.all_user_data.is_(True),
        ]
        if thread_id:
            selector_conditions.append(ForgetShield.thread_id == thread_id)
        if scope_type and scope_id:
            selector_conditions.append(
                (ForgetShield.scope_type == scope_type)
                & (ForgetShield.scope_id == scope_id)
            )
        if target_time is not None:
            # time_range shield 仅在目标时间落入范围内时生效
            selector_conditions.append(
                (ForgetShield.selector_type == "time_range")
                & (ForgetShield.time_from <= target_time)
                & (ForgetShield.time_to >= target_time)
            )

        all_conditions = entity_conditions + selector_conditions
        q = q.filter(or_(*all_conditions))

        # cutoff：仅在提供了目标时间时，限制为创建早于 cutoff 的数据
        if target_time is not None:
            q = q.filter(ForgetShield.cutoff_created_at >= target_time)

        return q.first() is not None

    # ── Tombstone 检查 ──

    @staticmethod
    def blocked_target_ids(
        session: Session,
        target_type: str,
        id_time_pairs: list[tuple[str, Any]],
    ) -> set[str]:
        """批量返回被 Shield（实体级/选择器级/all_user_data）或 Tombstone 屏蔽的 ID。

        供 UnifiedRetriever / RawHistoryExpander / 各读取路径 fail-closed 过滤。
        id_time_pairs: [(id, created_at_or_None), ...]；created_at 仅作 time_range
        判定的兜底，实际元数据（thread/scope/canonical_key）从各自表重载，以保证
        选择器级 Shield（scope/canonical_key/time_range）正确生效。

        返回值：应被过滤掉的（不可见）ID 集合。
        """
        if not id_time_pairs:
            return set()
        ids = [p[0] for p in id_time_pairs]

        # 全局 all_user_data shield 存在 → 全部屏蔽（fail-closed）
        global_block = (
            session.query(ForgetShield.id)
            .filter(
                ForgetShield.status == "active",
                ForgetShield.all_user_data.is_(True),
            )
            .first()
        )
        if global_block is not None:
            return {tid for tid in ids}

        # 重载各目标元数据（thread_id / scope / canonical_key / created_at）
        meta = ForgetVisibilityService._load_target_meta(session, target_type, ids)
        blocked: set[str] = set()

        # 1) 实体级 Shield（target_type/target_id 直接命中）
        shield_rows = (
            session.query(ForgetShield.target_id)
            .filter(
                ForgetShield.status == "active",
                ForgetShield.target_type == target_type,
                ForgetShield.target_id.in_(ids),
            )
            .all()
        )
        blocked.update(r[0] for r in shield_rows)

        # 2) 选择器级 Shield（thread / scope / canonical_key / time_range）
        if meta:
            thread_ids = {m["thread_id"] for m in meta if m["thread_id"]}
            if thread_ids:
                shielded_threads = {
                    r[0] for r in session.query(ForgetShield.thread_id).filter(
                        ForgetShield.status == "active",
                        ForgetShield.selector_type == "thread",
                        ForgetShield.thread_id.in_(thread_ids),
                    ).all()
                }
                for m in meta:
                    if m["thread_id"] in shielded_threads:
                        blocked.add(m["id"])

            scope_pairs = {
                (m["scope_type"], m["scope_id"])
                for m in meta if m["scope_type"] and m["scope_id"]
            }
            if scope_pairs:
                srows = session.query(
                    ForgetShield.scope_type, ForgetShield.scope_id,
                ).filter(
                    ForgetShield.status == "active",
                    ForgetShield.selector_type == "scope",
                ).all()
                shielded_scopes = {(r[0], r[1]) for r in srows}
                for m in meta:
                    if (m["scope_type"], m["scope_id"]) in shielded_scopes:
                        blocked.add(m["id"])

            cks = {m["canonical_key"] for m in meta if m["canonical_key"]}
            if cks:
                srows = session.query(ForgetShield.canonical_key).filter(
                    ForgetShield.status == "active",
                    ForgetShield.selector_type == "canonical_key",
                    ForgetShield.canonical_key.in_(cks),
                ).all()
                shielded_cks = {r[0] for r in srows}
                for m in meta:
                    if m["canonical_key"] in shielded_cks:
                        blocked.add(m["id"])

            time_shields = session.query(
                ForgetShield.time_from, ForgetShield.time_to,
            ).filter(
                ForgetShield.status == "active",
                ForgetShield.selector_type == "time_range",
            ).all()
            if time_shields:
                for m in meta:
                    ct = m["created_at"]
                    if ct is None:
                        continue
                    for tf, tt in time_shields:
                        if (tf is None or ct >= tf) and (tt is None or ct <= tt):
                            blocked.add(m["id"])
                            break

        # 3) Tombstone block_visibility
        blocked.update(
            ForgetVisibilityService.batch_is_tombstone_blocked(
                session, target_type, ids
            )
        )
        return blocked

    @staticmethod
    def _load_target_meta(
        session: Session, target_type: str, ids: list[str],
    ) -> list[dict[str, Any]]:
        """重载目标元数据（thread_id / scope / canonical_key / created_at）。

        不同 target_type 来自不同表；缺失目标忽略，返回字典列表。
        """
        from aiive.db.models import (
            MemoryRecord, Event, SegmentSummary, EpochCheckpoint,
            Segment, Epoch,
        )
        if target_type == "memory_record":
            # MemoryRecord 不带 thread_id（按 scope 归属），故 thread 选择器对
            # memory_record 不直接生效，仅 scope/canonical_key/time_range 生效。
            rows = session.query(
                MemoryRecord.id,
                MemoryRecord.scope_type, MemoryRecord.scope_id,
                MemoryRecord.canonical_key, MemoryRecord.created_at,
            ).filter(MemoryRecord.id.in_(ids)).all()
            return [
                {"id": r[0], "thread_id": None, "scope_type": r[1],
                 "scope_id": r[2], "canonical_key": r[3], "created_at": r[4]}
                for r in rows
            ]
        if target_type == "event":
            rows = session.query(
                Event.id, Event.thread_id, Event.created_at,
            ).filter(Event.id.in_(ids)).all()
            return [
                {"id": r[0], "thread_id": r[1], "scope_type": None,
                 "scope_id": None, "canonical_key": None, "created_at": r[2]}
                for r in rows
            ]
        if target_type == "segment_summary":
            rows = session.query(
                SegmentSummary.id, Segment.thread_id, SegmentSummary.created_at,
            ).join(Segment, Segment.id == SegmentSummary.segment_id).filter(
                SegmentSummary.id.in_(ids),
            ).all()
            return [
                {"id": r[0], "thread_id": r[1], "scope_type": None,
                 "scope_id": None, "canonical_key": None, "created_at": r[2]}
                for r in rows
            ]
        if target_type == "epoch_checkpoint":
            rows = session.query(
                EpochCheckpoint.id, Epoch.thread_id, EpochCheckpoint.created_at,
            ).join(Epoch, Epoch.id == EpochCheckpoint.epoch_id).filter(
                EpochCheckpoint.id.in_(ids),
            ).all()
            return [
                {"id": r[0], "thread_id": r[1], "scope_type": None,
                 "scope_id": None, "canonical_key": None, "created_at": r[2]}
                for r in rows
            ]
        return []

    @staticmethod
    def batch_is_tombstone_blocked(
        session: Session,
        target_type: str,
        target_ids: list[str],
    ) -> set[str]:
        """批量检查目标是否被 tombstone.block_visibility 屏蔽。

        Returns:
            被屏蔽的 target_id 集合（读取路径应 fail-closed 过滤掉）。
        """
        if not target_ids:
            return set()
        rows = (
            session.query(ForgetTombstone.target_id)
            .filter(
                ForgetTombstone.target_type == target_type,
                ForgetTombstone.target_id.in_(target_ids),
                ForgetTombstone.block_visibility.is_(True),
            )
            .all()
        )
        return {row.target_id for row in rows}

    @staticmethod
    def is_tombstone_visible(
        session: Session,
        target_type: str,
        target_id: str,
    ) -> bool:
        """单条检查：目标是否未被子 tombstone 屏蔽。"""
        blocked = (
            session.query(ForgetTombstone.id)
            .filter(
                ForgetTombstone.target_type == target_type,
                ForgetTombstone.target_id == target_id,
                ForgetTombstone.block_visibility.is_(True),
            )
            .first()
        )
        return blocked is None

    @staticmethod
    def is_tombstone_blocked_reingestion(
        session: Session,
        *,
        source_event_id: str | None = None,
        source_turn_record_id: str | None = None,
        canonical_key: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
    ) -> bool:
        """检查来源是否被子 tombstone.block_reingestion 拦截重新抽取。

        Args:
            source_event_id: 来源 Event ID
            source_turn_record_id: 来源 Turn ID
            canonical_key: 内容 canonical_key
        """
        q = session.query(ForgetTombstone.id).filter(
            ForgetTombstone.block_reingestion.is_(True),
        )

        conditions: list[Any] = []
        if source_event_id:
            conditions.append(ForgetTombstone.source_event_id == source_event_id)
        if source_turn_record_id:
            conditions.append(ForgetTombstone.source_turn_record_id == source_turn_record_id)
        if canonical_key and scope_type and scope_id:
            conditions.append(
                (ForgetTombstone.canonical_key == canonical_key)
                & (ForgetTombstone.scope_type == scope_type)
                & (ForgetTombstone.scope_id == scope_id)
            )

        if not conditions:
            return False
        return session.query(q.filter(or_(*conditions)).exists()).scalar()

    # ── 审计读取 ──

    @staticmethod
    def can_audit_read(
        session: Session,
        target_type: str,
        target_id: str,
    ) -> bool:
        """检查目标是否允许经授权审计模式读取。

        仅当 allow_audit_read=true 且 content_purged=false 时返回 True。
        """
        row = (
            session.query(
                ForgetTombstone.allow_audit_read,
                ForgetTombstone.content_purged,
            )
            .filter(
                ForgetTombstone.target_type == target_type,
                ForgetTombstone.target_id == target_id,
            )
            .first()
        )
        if row is None:
            return False  # 无 tombstone → 不在审计范围
        return bool(row.allow_audit_read) and not bool(row.content_purged)

    # ── 批量过滤器（供 UnifiedRetriever / ContextAssembler 使用） ──

    @staticmethod
    def filter_blocked_ids(
        session: Session,
        target_type: str,
        target_ids: list[str],
    ) -> list[str]:
        """过滤被 Shield 或 Tombstone 屏蔽的 ID，返回可见部分。"""
        blocked = ForgetVisibilityService.batch_is_tombstone_blocked(
            session, target_type, target_ids
        )
        return [tid for tid in target_ids if tid not in blocked]

    @staticmethod
    def filter_events(
        session: Session,
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """DEEP 历史回溯：过滤被 Shield/Tombstone 屏蔽的 Event。

        先查选择器级 Shield（thread_id/scope/time_range/all_user_data），
        再查实体级 Tombstone（block_visibility）。
        """
        if not events:
            return []

        def _event_id(e: dict[str, Any]) -> str | None:
            return e.get("id") or e.get("event_id")

        event_ids = [eid for e in events if (eid := _event_id(e))]

        # 选择器级 Shield：检查 thread 级别
        thread_ids = list({e.get("thread_id") for e in events if e.get("thread_id")})
        shielded_threads: set[str] = set()
        if thread_ids:
            rows = (
                session.query(ForgetShield.thread_id)
                .filter(
                    ForgetShield.status == "active",
                    ForgetShield.thread_id.in_(thread_ids),
                )
                .all()
            )
            shielded_threads = {row.thread_id for row in rows if row.thread_id}
        # all_user_data Shield 全局拦截
        has_all = (
            session.query(ForgetShield.id)
            .filter(ForgetShield.status == "active", ForgetShield.all_user_data.is_(True))
            .first()
        )
        if has_all:
            return []

        # Tombstone 实体级屏蔽
        blocked = ForgetVisibilityService.batch_is_tombstone_blocked(
            session, "event", event_ids,
        )
        return [
            e for e in events
            if _event_id(e) not in blocked
            and e.get("thread_id") not in shielded_threads
        ]

    @staticmethod
    def filter_memory_ids(
        session: Session,
        memory_ids: list[str],
    ) -> list[str]:
        """过滤被遗忘的 memory 记录（仅返回未被屏蔽的）。"""
        return ForgetVisibilityService.filter_blocked_ids(
            session, "memory_record", memory_ids
        )


