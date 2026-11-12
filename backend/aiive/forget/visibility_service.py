"""统一 fail-closed ForgetVisibilityService。

所有读取路径统一调用本服务，禁止各模块自行实现 forgotten 判断。
读取顺序：先查 Shield（选择器级），再查 Tombstone（实体级）。
异常时 fail-closed（默认不可见），不是 fail-open。

审计可见性规则：allow_raw_history && tombstone.allow_audit_read && !tombstone.content_purged
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, true
from sqlalchemy.orm import Session

from aiive.db.forget_models import ContentProvenanceRef, ForgetOperation, ForgetShield, ForgetTombstone


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
            # time_range shield 仅在目标时间落入范围内时生效；
            # 开区间（time_from/time_to 为 NULL）视为无界，NULL 比较不吞命中。
            selector_conditions.append(
                (ForgetShield.selector_type == "time_range")
                & or_(
                    ForgetShield.time_from.is_(None),
                    ForgetShield.time_from <= target_time,
                )
                & or_(
                    ForgetShield.time_to.is_(None),
                    ForgetShield.time_to >= target_time,
                )
            )

        all_conditions = entity_conditions + selector_conditions
        q = q.filter(or_(*all_conditions))

        # cutoff：仅在提供了目标时间时，限制为创建早于 cutoff 的数据
        if target_time is not None:
            q = q.filter(ForgetShield.cutoff_created_at >= target_time)

        return q.first() is not None

    @staticmethod
    def blocked_attachment_ids(session: Session, ids: list[str]) -> set[str]:
        """Batch Phase A check against the upload's own timestamp; raw history semantics."""
        from aiive.db.models import Attachment
        from sqlalchemy import and_
        if not ids:
            return set()
        try:
            rows = session.query(Attachment.id).join(ForgetShield, and_(
                ForgetShield.status == "active",
                ForgetShield.cutoff_created_at >= Attachment.created_at,
                or_(ForgetShield.all_user_data.is_(True), ForgetShield.thread_id == Attachment.thread_id,
                    and_(ForgetShield.selector_type == "time_range",
                         or_(ForgetShield.time_from.is_(None), ForgetShield.time_from <= Attachment.created_at),
                         or_(ForgetShield.time_to.is_(None), ForgetShield.time_to >= Attachment.created_at)),
                    and_(ForgetShield.scope_type == "thread", ForgetShield.scope_id == Attachment.thread_id)),
            )).join(ForgetOperation, ForgetOperation.id == ForgetShield.forget_operation_id).filter(
                Attachment.id.in_(ids), ForgetOperation.mode != "memory_only").distinct().all()
            return {row[0] for row in rows}
        except Exception:
            return set(ids)

    # ── Tombstone 检查 ──

    @staticmethod
    def _as_utc(dt: Any) -> datetime | None:
        """归一化为 aware UTC datetime；接受 datetime / ISO 字符串 / None。"""
        if dt is None:
            return None
        if isinstance(dt, str):
            try:
                dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None
        if not isinstance(dt, datetime):
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    @staticmethod
    def blocked_target_ids(
        session: Session,
        target_type: str,
        id_time_pairs: list[tuple[str, Any]],
        *,
        include_tombstones: bool = True,
        _expand_provenance: bool = True,
        _ignore_memory_only: bool = False,
    ) -> set[str]:
        """统一读取边界；查询失败时整批关闭，禁止部分解析成功后放行。"""
        try:
            return ForgetVisibilityService._blocked_target_ids(
                session, target_type, id_time_pairs,
                include_tombstones=include_tombstones,
                _expand_provenance=_expand_provenance,
                _ignore_memory_only=_ignore_memory_only,
            )
        except Exception:
            import logging

            logging.getLogger(__name__).exception("Forget visibility lookup failed")
            return {pair[0] for pair in id_time_pairs}

    @staticmethod
    def _blocked_target_ids(
        session: Session,
        target_type: str,
        id_time_pairs: list[tuple[str, Any]],
        *,
        include_tombstones: bool = True,
        _expand_provenance: bool = True,
        _ignore_memory_only: bool = False,
    ) -> set[str]:
        """批量返回被 Shield（实体级/选择器级/all_user_data）或 Tombstone 屏蔽的 ID。

        供 UnifiedRetriever / RawHistoryExpander / 各读取路径 fail-closed 过滤。
        id_time_pairs: [(id, created_at_or_None), ...]；created_at 仅作元数据缺失时
        的兜底，实际元数据（thread/scope/canonical_key/created_at）从各自表重载，
        以保证选择器级 Shield（scope/canonical_key/time_range）正确生效。

        cutoff 语义：所有选择器级 Shield（含 all_user_data）仅屏蔽创建时间
        <= shield.cutoff_created_at 的数据；遗忘操作之后新产生的数据不受屏蔽。
        创建时间无法确定时 fail-closed 屏蔽。

        返回值：应被过滤掉的（不可见）ID 集合。
        """
        if not id_time_pairs:
            return set()
        ids = [p[0] for p in id_time_pairs]
        provided_time: dict[str, Any] = {p[0]: p[1] for p in id_time_pairs}

        # 重载各目标元数据（thread_id / scope / canonical_key / created_at）
        meta = ForgetVisibilityService.load_target_meta(session, target_type, ids)
        meta_by_id = {m["id"]: m for m in meta}
        _as_utc = ForgetVisibilityService._as_utc

        def _created(tid: str) -> datetime | None:
            m = meta_by_id.get(tid)
            if m is not None and m.get("created_at") is not None:
                return _as_utc(m["created_at"])
            return _as_utc(provided_time.get(tid))

        def query(*entities: Any) -> Any:
            q = session.query(*entities)
            if _ignore_memory_only:
                q = q.filter(~ForgetShield.forget_operation_id.in_(
                    session.query(ForgetOperation.id).filter(ForgetOperation.mode == "memory_only"),
                ))
            return q

        blocked: set[str] = set()

        # 0) 全局 all_user_data shield：屏蔽 cutoff 之前创建的数据；
        #    创建时间未知 → fail-closed 屏蔽。
        global_cutoffs = [
            _as_utc(r[0]) for r in query(
                ForgetShield.cutoff_created_at,
            ).filter(
                ForgetShield.status == "active",
                ForgetShield.all_user_data.is_(True),
            ).all()
        ]
        if global_cutoffs:
            for tid in ids:
                ct = _created(tid)
                if ct is None or any(c is None or ct <= c for c in global_cutoffs):
                    blocked.add(tid)

        # 1) 实体级 Shield（target_type/target_id 直接命中，无 cutoff 语义）
        shield_rows = (
            query(ForgetShield.target_id)
            .filter(
                ForgetShield.status == "active",
                ForgetShield.target_type == target_type,
                ForgetShield.target_id.in_(ids),
            )
            .all()
        )
        # target_id 列可空；None 不是有效目标，直接跳过（同时收窄为 str）
        for row in shield_rows:
            shield_target_id = row[0]
            if shield_target_id is not None:
                blocked.add(shield_target_id)

        # 2) 选择器级 Shield（thread / scope / canonical_key / time_range），
        #    统一比较目标 created_at <= shield.cutoff_created_at。
        if meta:
            def _within_cutoff(ct: datetime | None, cutoff: Any) -> bool:
                cut = _as_utc(cutoff)
                if cut is None:
                    # 正常写入侧总有 cutoff；缺失时 fail-closed（视为覆盖）
                    return True
                if ct is None:
                    return True  # 创建时间未知 → fail-closed
                return ct <= cut

            thread_ids = {m["thread_id"] for m in meta if m["thread_id"]}
            if thread_ids:
                trows = query(
                    ForgetShield.thread_id, ForgetShield.cutoff_created_at,
                ).filter(
                    ForgetShield.status == "active",
                    ForgetShield.selector_type == "thread",
                    ForgetShield.thread_id.in_(thread_ids),
                ).all()
                cutoffs_by_thread: dict[str, list[Any]] = {}
                for tid, cut in trows:
                    if tid is None:
                        continue  # 选择器行的 thread_id 可空，空值不构成 shield
                    cutoffs_by_thread.setdefault(tid, []).append(cut)
                for m in meta:
                    cuts = cutoffs_by_thread.get(m["thread_id"] or "")
                    if not cuts:
                        continue
                    ct = _as_utc(m["created_at"])
                    if any(_within_cutoff(ct, c) for c in cuts):
                        blocked.add(m["id"])

            scope_pairs = {
                (m["scope_type"], m["scope_id"])
                for m in meta if m["scope_type"] and m["scope_id"]
            }
            if scope_pairs:
                srows = query(
                    ForgetShield.scope_type, ForgetShield.scope_id,
                    ForgetShield.cutoff_created_at,
                ).filter(
                    ForgetShield.status == "active",
                    ForgetShield.selector_type == "scope",
                ).all()
                cutoffs_by_scope: dict[tuple[Any, Any], list[Any]] = {}
                for st, si, cut in srows:
                    cutoffs_by_scope.setdefault((st, si), []).append(cut)
                for m in meta:
                    cuts = cutoffs_by_scope.get((m["scope_type"], m["scope_id"]))
                    if not cuts:
                        continue
                    ct = _as_utc(m["created_at"])
                    if any(_within_cutoff(ct, c) for c in cuts):
                        blocked.add(m["id"])

            cks = {m["canonical_key"] for m in meta if m["canonical_key"]}
            if cks:
                krows = query(
                    ForgetShield.canonical_key, ForgetShield.cutoff_created_at,
                ).filter(
                    ForgetShield.status == "active",
                    ForgetShield.selector_type == "canonical_key",
                    ForgetShield.canonical_key.in_(cks),
                ).all()
                cutoffs_by_ck: dict[str, list[Any]] = {}
                for ck, cut in krows:
                    if ck is None:
                        continue  # 选择器行的 canonical_key 可空，空值不构成 shield
                    cutoffs_by_ck.setdefault(ck, []).append(cut)
                for m in meta:
                    cuts = cutoffs_by_ck.get(m["canonical_key"] or "")
                    if not cuts:
                        continue
                    ct = _as_utc(m["created_at"])
                    if any(_within_cutoff(ct, c) for c in cuts):
                        blocked.add(m["id"])

            time_shields = query(
                ForgetShield.time_from, ForgetShield.time_to,
                ForgetShield.cutoff_created_at,
            ).filter(
                ForgetShield.status == "active",
                ForgetShield.selector_type == "time_range",
            ).all()
            if time_shields:
                for m in meta:
                    ct = _as_utc(m["created_at"])
                    if ct is None:
                        # 创建时间未知 → fail-closed
                        blocked.add(m["id"])
                        continue
                    for tf, tt, cut in time_shields:
                        tf_u, tt_u = _as_utc(tf), _as_utc(tt)
                        if (
                            (tf_u is None or ct >= tf_u)
                            and (tt_u is None or ct <= tt_u)
                            and _within_cutoff(ct, cut)
                        ):
                            blocked.add(m["id"])
                            break

        # 3) Tombstone block_visibility（防重抽判定时排除：Tombstone 的
        #    block_visibility 与 block_reingestion 是独立语义，不能把可见性
        #    屏蔽混作防重抽依据）
        if include_tombstones:
            blocked.update(
                ForgetVisibilityService.batch_is_tombstone_blocked(
                    session, target_type, ids, ignore_memory_only=_ignore_memory_only
                )
            )

        # 4/5) Event 扩展规则（仅 target_type == "event"）：
        #   - 所属 TurnRecord 被 Shield/Tombstone 屏蔽的全部子 Event；
        #   - 作为「已被遗忘 MemoryRecord」来源的 Event（Memory → Event Provenance）。
        #   覆盖宽选择器 / 超限 ID：Phase A 不为屏蔽来源 Event 而枚举全部记录，
        #   改由读取侧经 Provenance + 选择器 Shield 即时判定。
        #   Provenance 侧复用 blocked_target_ids("memory_record", ...)，其内部
        #   仅在 target_type == "event" 时做本扩展，故不会递归。
        if _expand_provenance and target_type in {"event", "turn_record"}:
            from aiive.db.models import Event, TurnRecord
            if target_type == "turn_record":
                owners = {token: token for token in ids}
            else:
                owners = {event.id: turn.id for event, turn in session.query(Event, TurnRecord).join(
                    TurnRecord, (TurnRecord.thread_id == Event.thread_id) & (TurnRecord.turn_id == Event.turn_id)
                ).filter(Event.id.in_(ids)).all()}
            refs = session.query(ContentProvenanceRef).filter(
                ContentProvenanceRef.owner_type == "attachment_context",
                ContentProvenanceRef.owner_id.in_(set(owners.values())),
                ContentProvenanceRef.source_type == "event",
            ).all()
            source_ids = {r.source_id for r in refs}
            if source_ids:
                source_blocked = ForgetVisibilityService.blocked_target_ids(
                    session, "event", [(i, None) for i in source_ids],
                    include_tombstones=include_tombstones, _expand_provenance=False,
                    _ignore_memory_only=_ignore_memory_only)
                blocked_owners = {r.owner_id for r in refs if r.source_id in source_blocked}
                blocked.update(token for token, owner in owners.items() if owner in blocked_owners)
        if target_type == "memory_record" and _expand_provenance:
            blocked.update(ForgetVisibilityService._source_event_blocked_memory_ids(
                session, ids, include_tombstones=include_tombstones,
            ))
        if target_type == "event":
            blocked.update(
                ForgetVisibilityService._turn_blocked_event_ids(
                    session, ids, include_tombstones=include_tombstones,
                    ignore_memory_only=_ignore_memory_only,
                )
            )
            if _expand_provenance:
                blocked.update(
                    ForgetVisibilityService._source_memory_blocked_event_ids(
                        session, ids, include_tombstones=include_tombstones, ignore_memory_only=_ignore_memory_only,
                    )
                )

        if _expand_provenance and target_type in {"segment_summary", "epoch_checkpoint"}:
            from aiive.db.models import SegmentSummary, EpochCheckpoint
            from aiive.forget.durable_sources import summary_sources_visible
            if target_type == "segment_summary":
                rows = session.query(SegmentSummary).filter(SegmentSummary.id.in_(ids)).all()
                blocked.update(set(ids) - {r.id for r in rows})
                blocked.update(r.id for r in rows if not summary_sources_visible(session, r))
            else:
                rows = session.query(EpochCheckpoint).filter(EpochCheckpoint.id.in_(ids)).all()
                blocked.update(set(ids) - {r.id for r in rows})
                for checkpoint in rows:
                    segments = checkpoint.source_segment_ids
                    if not isinstance(segments, list) or not segments or any(not isinstance(i, str) or not i for i in segments):
                        blocked.add(checkpoint.id)
                        continue
                    summaries = session.query(SegmentSummary).filter(SegmentSummary.segment_id.in_(segments)).all()
                    if set(segments) - {r.segment_id for r in summaries} or ForgetVisibilityService.blocked_target_ids(
                        session, "segment_summary", [(r.id, r.created_at) for r in summaries],
                    ):
                        blocked.add(checkpoint.id)

        return blocked

    @staticmethod
    def _source_event_blocked_memory_ids(
        session: Session, memory_ids: list[str], *, include_tombstones: bool,
    ) -> set[str]:
        """Memory → 全部 Event 来源闭包；不把 Memory 屏蔽再反射到兄弟 Memory。

        无来源仅允许持久化提案证明为 manual_memory_api 独立 create 的记录。
        后续混入无来源提案不继承该豁免；空引用、未知类型和悬空来源关闭。
        """
        from aiive.db.models import Event, MemoryProposal, MemoryRecord

        records = session.query(MemoryRecord).filter(MemoryRecord.id.in_(memory_ids)).all()
        refs = session.query(ContentProvenanceRef).filter(
            ContentProvenanceRef.owner_type == "memory_record",
            ContentProvenanceRef.owner_id.in_(memory_ids),
        ).all()
        by_memory: dict[str, set[str]] = {r.id: set() for r in records}
        blocked = set(memory_ids) - set(by_memory)
        for ref in refs:
            if ref.source_type != "event" or not ref.source_id.strip():
                blocked.add(ref.owner_id)
            else:
                by_memory[ref.owner_id].add(ref.source_id)
        proposals = session.query(MemoryProposal).filter(
            MemoryProposal.final_memory_id.in_(memory_ids),
        ).all()
        proposals_by_memory: dict[str, list[Any]] = {}
        for proposal in proposals:
            proposals_by_memory.setdefault(proposal.final_memory_id or "", []).append(proposal)
        for record in records:
            sources = by_memory[record.id]
            if record.source_event_id and record.source_event_id not in sources:
                blocked.add(record.id)
            proofs = proposals_by_memory.get(record.id, [])
            for proof in proofs:
                declared = proof.source_event_ids
                evidence = proof.evidence
                if declared is not None:
                    if not isinstance(declared, list) or any(not isinstance(e, str) or not e.strip() for e in declared):
                        blocked.add(record.id)
                    elif set(declared) - sources:
                        blocked.add(record.id)
                if evidence is not None:
                    if not isinstance(evidence, list) or any(not isinstance(e, dict) for e in evidence):
                        blocked.add(record.id)
                    elif any(e.get("source_event_id") and e["source_event_id"] not in sources for e in evidence):
                        blocked.add(record.id)
            if not sources:
                if not proofs or not any(p.final_operation == "create" for p in proofs) or not all(
                    p.extractor_name == "manual_memory_api"
                    and p.execution_mode == "user_required"
                    and p.source_event_ids == [] and p.evidence == []
                    and not p.source_turn_id and not p.source_turn_record_id
                    and p.final_operation in {"create", "reinforce", "merge"}
                    for p in proofs
                ):
                    blocked.add(record.id)
        event_ids = set().union(*by_memory.values()) if by_memory else set()
        events = session.query(Event.id, Event.created_at).filter(Event.id.in_(event_ids)).all() if event_ids else []
        unsafe = event_ids - {row[0] for row in events if ForgetVisibilityService._as_utc(row[1]) is not None}
        unsafe.update(ForgetVisibilityService.blocked_target_ids(
            session, "event", [(row[0], row[1]) for row in events],
            include_tombstones=include_tombstones, _expand_provenance=False,
            _ignore_memory_only=True,
        ))
        blocked.update(mid for mid, sources in by_memory.items() if sources & unsafe)
        return blocked

    @staticmethod
    def load_target_meta(
        session: Session, target_type: str, ids: list[str],
    ) -> list[dict[str, Any]]:
        """重载目标元数据（thread_id / scope / canonical_key / created_at）。

        不同 target_type 来自不同表；缺失目标忽略，返回字典列表。
        """
        from aiive.db.models import (
            MemoryRecord, Event, SegmentSummary, EpochCheckpoint,
            Segment, Epoch, TurnRecord,
        )
        if target_type == "turn_record":
            # 纯 Turn 来源防重抽：用 TurnRecord.created_at 应用选择器 Shield 的
            # cutoff（Forget 之后产生的新 Turn 不应被旧 Shield 阻断）。
            rows = session.query(
                TurnRecord.id, TurnRecord.thread_id, TurnRecord.created_at, TurnRecord.turn_id,
            ).filter(or_(TurnRecord.id.in_(ids), TurnRecord.turn_id.in_(ids))).all()
            return [
                {"id": token, "thread_id": r[1], "scope_type": None,
                 "scope_id": None, "canonical_key": None, "created_at": r[2]}
                for r in rows for token in {r[0], r[3]} if token in ids
            ]
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

    # ── Event 扩展屏蔽：TurnRecord 覆盖 + Memory → Event Provenance ──

    @staticmethod
    def _turn_tokens_by_event(
        session: Session, event_ids: list[str],
    ) -> dict[str, set[str]]:
        """每个 Event 对应的「Turn 标识 token」集合。

        token 含 Event.turn_id（逻辑 turn id），以及匹配到的 TurnRecord 的
        id / turn_id。turn_record 的 Shield / Tombstone.target_id 可能是逻辑
        turn_id，也可能是 TurnRecord.id（历史数据两种写法并存），故同时展开。
        """
        from aiive.db.models import Event, TurnRecord

        result: dict[str, set[str]] = {}
        if not event_ids:
            return result
        ev_rows = (
            session.query(Event.id, Event.turn_id)
            .filter(Event.id.in_(event_ids))
            .all()
        )
        logical_by_event = {r[0]: r[1] for r in ev_rows}
        logical_ids = {v for v in logical_by_event.values() if v}

        tr_tokens: dict[str, set[str]] = {}
        if logical_ids:
            tr_rows = (
                session.query(TurnRecord.id, TurnRecord.turn_id)
                .filter(
                    or_(
                        TurnRecord.turn_id.in_(logical_ids),
                        TurnRecord.id.in_(logical_ids),
                    )
                )
                .all()
            )
            for tr_id, tr_turn_id in tr_rows:
                toks = {x for x in (tr_id, tr_turn_id) if x}
                for key in (tr_id, tr_turn_id):
                    if key:
                        tr_tokens.setdefault(key, set()).update(toks)

        for eid, lid in logical_by_event.items():
            toks: set[str] = set()
            if lid:
                toks.add(lid)
                toks.update(tr_tokens.get(lid, set()))
            result[eid] = toks
        return result

    @staticmethod
    def turn_tokens_by_event(
        session: Session, event_ids: list[str],
    ) -> dict[str, set[str]]:
        """公开入口：返回每个 Event 对应的 Turn 标识 token 集合。

        供 Verify / 防重抽等跨模块判定使用，避免直接依赖私有实现。
        """
        return ForgetVisibilityService._turn_tokens_by_event(session, event_ids)

    @staticmethod
    def _turn_blocked_event_ids(
        session: Session, event_ids: list[str], *, include_tombstones: bool = True,
        ignore_memory_only: bool = False,
    ) -> set[str]:
        """返回所属 TurnRecord 被 Shield / Tombstone 屏蔽的 Event id 集合。

        Turn 被遗忘后，其全部子 Event 在清理完成前即须不可见（含 TurnRecord
        Tombstone 映射），而不依赖 Cascade 是否已完成内容清理。
        """
        tokens_by_event = ForgetVisibilityService._turn_tokens_by_event(
            session, event_ids,
        )
        if not tokens_by_event:
            return set()
        all_tokens: set[str] = set()
        for toks in tokens_by_event.values():
            all_tokens.update(toks)
        if not all_tokens:
            return set()

        shielded = {
            r[0] for r in session.query(ForgetShield.target_id).filter(
                ForgetShield.status == "active",
                ForgetShield.target_type == "turn_record",
                ForgetShield.target_id.in_(all_tokens),
                ~ForgetShield.forget_operation_id.in_(session.query(ForgetOperation.id).filter(
                    ForgetOperation.mode == "memory_only",
                )) if ignore_memory_only else true(),
            ).all() if r[0]
        }
        tombstoned: set[str] = set()
        if include_tombstones:
            tombstoned = {
                r[0] for r in session.query(ForgetTombstone.target_id).filter(
                    ForgetTombstone.target_type == "turn_record",
                    ForgetTombstone.target_id.in_(all_tokens),
                    ~ForgetTombstone.forget_operation_id.in_(session.query(ForgetOperation.id).filter(
                        ForgetOperation.mode == "memory_only",
                    )) if ignore_memory_only else true(),
                    ForgetTombstone.block_visibility.is_(True),
                ).all() if r[0]
            }
        blocked_tokens = shielded | tombstoned
        if not blocked_tokens:
            return set()
        return {
            eid for eid, toks in tokens_by_event.items()
            if toks & blocked_tokens
        }

    @staticmethod
    def _source_memory_blocked_event_ids(
        session: Session, event_ids: list[str], *, include_tombstones: bool = True, ignore_memory_only: bool = False,
    ) -> set[str]:
        """返回「作为被屏蔽 MemoryRecord 来源」的 Event id 集合。

        Memory → Event 关系取自 ContentProvenanceRef（持久，Evidence 被删除后
        仍存活）。复用 blocked_target_ids("memory_record", ...) 应用全部实体级 /
        选择器级 Shield（含 cutoff）与 Tombstone 规则，以覆盖宽选择器。
        该调用以 target_type="memory_record" 进入，不会再次触发 Event 扩展，
        因此无递归。
        """
        if not event_ids:
            return set()
        rows = (
            session.query(
                ContentProvenanceRef.source_id, ContentProvenanceRef.owner_id,
            )
            .filter(
                ContentProvenanceRef.owner_type == "memory_record",
                ContentProvenanceRef.source_type == "event",
                ContentProvenanceRef.source_id.in_(event_ids),
            )
            .all()
        )
        if not rows:
            return set()
        memories_by_event: dict[str, list[str]] = {}
        mem_ids: set[str] = set()
        for src_id, owner_id in rows:
            memories_by_event.setdefault(src_id, []).append(owner_id)
            mem_ids.add(owner_id)
        blocked_memories = ForgetVisibilityService.blocked_target_ids(
            session, "memory_record", [(m, None) for m in mem_ids],
            include_tombstones=include_tombstones,
            _expand_provenance=False, _ignore_memory_only=ignore_memory_only,
        )
        if not blocked_memories:
            return set()
        return {
            eid for eid, mids in memories_by_event.items()
            if any(m in blocked_memories for m in mids)
        }

    @staticmethod
    def batch_is_tombstone_blocked(
        session: Session,
        target_type: str,
        target_ids: list[str],
        *, ignore_memory_only: bool = False,
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
                ~ForgetTombstone.forget_operation_id.in_(session.query(ForgetOperation.id).filter(
                    ForgetOperation.mode == "memory_only",
                )) if ignore_memory_only else true(),
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
        # 1) 活动 Shield：Phase A 生效后、Cascade 尚未运行前即必须阻断重抽。
        #    Shield 没有 block_visibility / block_reingestion 之分，active 即同时
        #    承担可见性屏蔽与防重抽；Tombstone.block_reingestion 仍是独立的长期
        #    依据（见下方查询），两者不得互相替代。
        if source_event_id and ForgetVisibilityService.blocked_target_ids(
            session,
            "event",
            [(source_event_id, None)],
            include_tombstones=False,
        ):
            return True
        turn_source_tokens: set[str] = set()
        turn_source_event_ids: list[str] = []
        if source_turn_record_id:
            from aiive.db.models import Event, TurnRecord

            tokens: set[str] = {source_turn_record_id}
            resolved_ids: set[str] = set()
            tr_rows = session.query(TurnRecord.id, TurnRecord.turn_id).filter(
                or_(
                    TurnRecord.id == source_turn_record_id,
                    TurnRecord.turn_id == source_turn_record_id,
                )
            ).all()
            for tr_id, tr_turn_id in tr_rows:
                if tr_id:
                    tokens.add(tr_id)
                    resolved_ids.add(tr_id)
                if tr_turn_id:
                    tokens.add(tr_turn_id)

            # 实体级 turn Shield：TurnRecord.id 与逻辑 turn_id 两种历史写法都匹配。
            # 纯 Turn 来源（无任何 Event ID）只能靠这里拦截。
            if session.query(ForgetShield.id).filter(
                ForgetShield.status == "active",
                ForgetShield.target_type == "turn_record",
                ForgetShield.target_id.in_(tokens),
            ).first() is not None:
                return True

            # 选择器级 Shield（thread / all_user_data / time_range）：用
            # TurnRecord.created_at 应用 cutoff —— Forget 之后产生的新 Turn
            # 创建时间晚于 cutoff，不会被旧 Shield 阻断。
            if resolved_ids and ForgetVisibilityService.blocked_target_ids(
                session,
                "turn_record",
                [(tid, None) for tid in sorted(resolved_ids)],
                include_tombstones=False,
            ):
                return True
            turn_source_tokens = set(tokens)
            turn_event_ids = [
                r[0] for r in session.query(Event.id).filter(
                    Event.turn_id.in_(tokens),
                ).all()
            ]
            turn_source_event_ids = list(turn_event_ids)
            if turn_event_ids and ForgetVisibilityService.blocked_target_ids(
                session,
                "event",
                [(eid, None) for eid in turn_event_ids],
                include_tombstones=False,
            ):
                return True

        q = session.query(ForgetTombstone.id).filter(
            ForgetTombstone.block_reingestion.is_(True),
        )

        conditions: list[Any] = []
        if source_event_id:
            conditions.append(ForgetTombstone.source_event_id == source_event_id)

            # Memory → Event Provenance：被遗忘 MemoryRecord 的来源 Event 不得
            # 被后台 Memory Extraction 重新抽取（MemoryRecord 是记忆真相源）。
            mem_ids = [
                r[0] for r in session.query(ContentProvenanceRef.owner_id).filter(
                    ContentProvenanceRef.owner_type == "memory_record",
                    ContentProvenanceRef.source_type == "event",
                    ContentProvenanceRef.source_id == source_event_id,
                ).all()
            ]
            if mem_ids:
                conditions.append(
                    (ForgetTombstone.target_type == "memory_record")
                    & (ForgetTombstone.target_id.in_(mem_ids))
                )

            # 来源 Event 所属 Turn 的 turn_record Tombstone 也应拦截（Turn 被
            # 遗忘时其子 Event 未必各自持有 event Tombstone）。
            turn_tokens = ForgetVisibilityService._turn_tokens_by_event(
                session, [source_event_id],
            ).get(source_event_id, set())
            if turn_tokens:
                conditions.append(
                    or_(
                        ForgetTombstone.source_turn_record_id.in_(turn_tokens),
                        (ForgetTombstone.target_type == "turn_record")
                        & (ForgetTombstone.target_id.in_(turn_tokens)),
                    )
                )
        if source_turn_record_id:
            # 同时匹配 TurnRecord.id 与逻辑 turn_id 两种历史写法（Phase A 的
            # turn Tombstone 两种都可能出现），并覆盖该 Turn 子 Event 的
            # event Tombstone（history_only 遗忘时只有事件级 tombstone）。
            conditions.append(
                or_(
                    ForgetTombstone.source_turn_record_id.in_(turn_source_tokens),
                    (ForgetTombstone.target_type == "turn_record")
                    & (ForgetTombstone.target_id.in_(turn_source_tokens)),
                )
            )
            if turn_source_event_ids:
                conditions.append(
                    ForgetTombstone.source_event_id.in_(turn_source_event_ids)
                )
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

        fail-closed：必须存在至少一条 tombstone，且**所有** tombstone 均
        allow_audit_read=true 且 content_purged=false 才返回 True。同一对象存在
        多个互相冲突的 tombstone（例如两次 Forget，其中一次已不可逆清理）时，
        任何不允许审计或已清理的记录都优先拒绝，不能因 .first() 的返回顺序
        造成权限绕过。
        """
        rows = (
            session.query(
                ForgetTombstone.allow_audit_read,
                ForgetTombstone.content_purged,
            )
            .filter(
                ForgetTombstone.target_type == target_type,
                ForgetTombstone.target_id == target_id,
            )
            .all()
        )
        if not rows:
            return False  # 无 tombstone → 不在审计范围
        return all(
            bool(row.allow_audit_read) and not bool(row.content_purged)
            for row in rows
        )

    # ── 批量过滤器（供 UnifiedRetriever / ContextAssembler 使用） ──

    @staticmethod
    def filter_blocked_ids(
        session: Session,
        target_type: str,
        target_ids: list[str],
    ) -> list[str]:
        """过滤被 Shield（实体级/选择器级/all_user_data，含 cutoff）或
        Tombstone 屏蔽的 ID，返回可见部分。"""
        blocked = ForgetVisibilityService.blocked_target_ids(
            session, target_type, [(tid, None) for tid in target_ids],
        )
        return [tid for tid in target_ids if tid not in blocked]

    @staticmethod
    def filter_events(
        session: Session,
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """DEEP 历史回溯：过滤被 Shield/Tombstone 屏蔽的 Event。

        统一委托 blocked_target_ids：覆盖实体级 Shield、选择器级 Shield
        （thread/scope/time_range/all_user_data，均含 cutoff 语义）与
        Tombstone.block_visibility。事件元数据（thread_id/created_at）从
        events 表回源加载；字典自带 created_at 仅作元数据缺失时的兜底。
        """
        if not events:
            return []

        def _event_id(e: dict[str, Any]) -> str | None:
            return e.get("id") or e.get("event_id")

        pairs: list[tuple[str, Any]] = []
        for e in events:
            eid = _event_id(e)
            if eid:
                pairs.append((eid, e.get("created_at")))

        blocked = ForgetVisibilityService.blocked_target_ids(
            session, "event", pairs,
        )
        return [e for e in events if _event_id(e) not in blocked]

    @staticmethod
    def filter_memory_ids(
        session: Session,
        memory_ids: list[str],
    ) -> list[str]:
        """过滤被遗忘的 memory 记录（仅返回未被屏蔽的）。"""
        return ForgetVisibilityService.filter_blocked_ids(
            session, "memory_record", memory_ids
        )


