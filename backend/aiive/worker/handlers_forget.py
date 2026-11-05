"""Phase 6A Forget Saga Outbox handlers。

注册:
  - forget_cascade            Phase B/J: Target/Dependency/Batch 物化
  - forget_rebuild_dependencies  Phase C/K/L: Summary/Checkpoint 重建 + Evidence 重算
  - forget_purge              Phase E/M: 内容 scrub + 物理删除
  - forget_verify             Phase N: ForgetVerifier
  - forget_reconcile          补发遗漏 Job
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from aiive.db.base import SessionLocal
from aiive.db.forget_models import (
    ContentProvenanceRef,
    ForgetAction,
    ForgetBatch,
    ForgetDependency,
    ForgetOperation,
    ForgetSelectorManifest,
    ForgetStageRun,
    ForgetTarget,
    ForgetTombstone,
)
from aiive.config import settings
from aiive.db.models import (
    Epoch,
    EpochCheckpoint,
    Event,
    MemoryEvidence,
    MemoryProposal as MemoryProposalModel,
    MemoryRecord,
    MemoryVectorProjection,
    OutboxJob,
    RetrievalIndexEntry,
    Segment,
    SegmentSummary,
    TurnRecord,
)
from aiive.worker.outbox_dto import (
    ClaimedJob,
    HandlerOutcome,
    HandlerResult,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 200  # 每批处理目标数


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _non_negative_int(value: Any, default: int = 0) -> int:
    """收敛 LLM/JSON 派生的计数字段，拒绝容器、布尔值和负数。"""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(0, parsed)


# selector_type → ForgetTarget.target_type 映射（turn/event 选择器不得写成 memory_record）
_SELECTOR_TARGET_TYPE: dict[str, str] = {
    "turn_ids": "turn_record",
    "event_ids": "event",
}


def _selector_target_type(selector_type: str) -> str:
    return _SELECTOR_TARGET_TYPE.get(selector_type, "memory_record")


def _normalize_turn_tokens(db: Session, turn_ids: list[str]) -> set[str]:
    """把 TurnRecord.id / turn_id 两种输入统一展开为可匹配的 token 集合。

    Phase A 可能收到 TurnRecord 数据库主键（id）或业务逻辑 turn_id；Event.turn_id
    只与后者同名关联。这里按任一 token 反查 TurnRecord，并同时返回 id 与 turn_id，
    保证依赖发现、子 Event Tombstone 与正文清理都不依赖调用方使用哪一种 ID。
    """
    from sqlalchemy import or_

    tokens = {tid for tid in turn_ids if tid}
    if not tokens:
        return tokens
    rows = db.query(TurnRecord.id, TurnRecord.turn_id).filter(
        or_(
            TurnRecord.id.in_(tokens),
            TurnRecord.turn_id.in_(tokens),
        )
    ).all()
    for tr_id, tr_turn_id in rows:
        if tr_id:
            tokens.add(tr_id)
        if tr_turn_id:
            tokens.add(tr_turn_id)
    return tokens


def _summaries_containing_event(db: Session, ev_id: str) -> list[SegmentSummary]:
    """按 source_event_ids 包含关系查 SegmentSummary（方言兼容）。

    PostgreSQL 用 jsonb @> 包含运算；SQLite 等其他方言无 JSONB 类型，
    先 LIKE 预筛再 Python 精确过滤。
    """
    if db.get_bind().dialect.name == "postgresql":
        return (
            db.query(SegmentSummary)
            .filter(cast(SegmentSummary.source_event_ids, JSONB).contains([ev_id]))
            .all()
        )
    from sqlalchemy import String as _SAString
    candidates = (
        db.query(SegmentSummary)
        .filter(cast(SegmentSummary.source_event_ids, _SAString).like(f"%{ev_id}%"))
        .all()
    )
    return [s for s in candidates if ev_id in (s.source_event_ids or [])]


def _enqueue_index_tombstone(db: Session, source_type: str, source_id: str, marker: str) -> None:
    """入队 tombstone 型 retrieval refresh（幂等）：使旧版 Summary/Checkpoint
    的索引条目不可检索。event_type='source.tombstoned' 在 indexing_service 的
    _TOMBSTONE_EVENTS 中，消费端会对该 source 执行 tombstone_by_source。
    """
    op_id = f"retrieval_refresh:{source_type}:{source_id}:{marker}"
    existing = db.query(OutboxJob).filter(OutboxJob.operation_id == op_id).first()
    if existing is not None:
        return
    db.add(OutboxJob(
        operation_id=op_id,
        job_type="retrieval_index_refresh",
        status="pending",
        payload={
            "schema_version": 1,
            "source_type": source_type,
            "source_id": source_id,
            "event_type": "source.tombstoned",
        },
        max_retries=3,
    ))


# ═══════════════════════════════════════════════════════════════════
# forget_cascade — Phase B/J: Target / Dependency / Batch 物化
# ═══════════════════════════════════════════════════════════════════


def handle_forget_cascade(claimed: ClaimedJob) -> HandlerResult:
    """Phase B/J: 从 selector_payload 物化 Target → Dependency → Batch。

    CONTINUE 实现：每次执行最多处理一批 BATCH_SIZE 个新 target 或依赖，
    未完成则返回 CONTINUE 让 Worker 稍后再次 claim 继续。
    """
    payload = claimed.payload
    operation_id = payload.get("forget_operation_id", "")
    if not operation_id:
        return HandlerResult(HandlerOutcome.NON_RETRYABLE, "missing forget_operation_id")

    db = SessionLocal()
    try:
        operation = db.query(ForgetOperation).filter_by(id=operation_id).first()
        if not operation:
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "operation not found")

        manifest = (
            db.query(ForgetSelectorManifest)
            .filter_by(forget_operation_id=operation_id)
            .first()
        )
        if not manifest:
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "selector manifest not found")

        selector_payload: dict[str, Any] = manifest.selector_payload
        selector_type = selector_payload.get("selector_type", "")
        cutoff = manifest.cutoff_created_at

        # ── 查找或创建 cascade Batch ──
        batch = _find_or_create_cascade_batch(db, operation_id, selector_type)

        # ── 阶段 1: 物化 Target ──
        if batch.status == "pending":
            result = _materialize_targets(
                db, operation_id, selector_payload, cutoff, batch,
            )
            if result is not None:
                db.close()
                return result

        # ── 阶段 2: 为所有已物化的 Target 写 Tombstone + 置 forgotten ──
        if batch.status == "pending":
            _write_tombstones_for_targets(db, operation_id)

        # ── 阶段 3: 发现 Dependency ──
        target_ids = _get_pending_target_ids(db, operation_id, batch)
        if target_ids:
            result = _discover_dependencies(db, operation_id, target_ids, batch)
            if result is not None:
                db.close()
                return result

        # ── 全部完成 ──
        batch.status = "done"
        batch.updated_at = _utcnow()
        # Enqueue next stage（必须在 commit 之前，同事务）
        _enqueue_next_stage(db, operation_id, "forget_rebuild_dependencies")
        db.commit()

        return HandlerResult(HandlerOutcome.COMPLETED, "cascade_completed")
    except Exception as exc:
        db.rollback()
        logger.exception("forget_cascade failed")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"cascade: {exc}")
    finally:
        db.close()


# ── Batch 管理 ──


def _find_or_create_cascade_batch(
    db: Session, operation_id: str, selector_type: str,
) -> ForgetBatch:
    """查找已有 cascade batch 或创建新 batch。"""
    batch = (
        db.query(ForgetBatch)
        .filter_by(
            forget_operation_id=operation_id,
            stage="cascade",
            dependency_type=selector_type,
            batch_no=0,
        )
        .first()
    )
    if batch:
        return batch

    batch = ForgetBatch(
        id=_generate_id(),
        forget_operation_id=operation_id,
        stage="cascade",
        dependency_type=selector_type,
        batch_no=0,
        cursor_lane="target",
        status="pending",
    )
    db.add(batch)
    db.flush()
    return batch


# ── Phase 1: 物化 Target ──


def _materialize_targets(
    db: Session,
    operation_id: str,
    selector_payload: dict[str, Any],
    cutoff: datetime,
    batch: ForgetBatch,
) -> HandlerResult | None:
    """从 selector_payload 物化 forget_targets（有界批次，keyset 游标续跑）。

    spec P/§10：禁止数据库 offset；使用复合 keyset 游标。
    宽选择器（all_user_data / thread / canonical_key / scope / time_range）
    按 memory_record 实际枚举 Target，确保后续 scrub 能处理全部受影响内容。
    """
    selector_type = selector_payload.get("selector_type", "")
    now = _utcnow()
    cursor = batch.cursor_start_json

    # keyset 分页加载目标 ID
    all_ids: list[str] = _resolve_selector_ids(
        db, selector_payload, cutoff, cursor, BATCH_SIZE,
    )

    if not all_ids:
        return None

    # 创建 target（按 selector_type 写正确的 target_type：
    # turn_ids → turn_record，event_ids → event，其余 → memory_record）
    target_type = _selector_target_type(selector_type)
    for tid in all_ids:
        _ensure_target(db, operation_id, target_type, tid, batch.batch_no, now)

    # 更新 keyset 游标
    if selector_type in ("memory_ids", "turn_ids", "event_ids"):
        # inline IDs：记录 Python 切片 offset
        offset = cursor.get("offset", 0) if cursor else 0
        batch.cursor_start_json = {"offset": offset + len(all_ids)}
    else:
        # 宽选择器：记录 keyset（最后一行的 created_at + id）
        last_row = (
            db.query(MemoryRecord.created_at)
            .filter(MemoryRecord.id == all_ids[-1])
            .first()
        )
        batch.cursor_start_json = {
            "last_created_at": (
                last_row[0].isoformat() if last_row and last_row[0] else ""
            ),
            "last_id": all_ids[-1],
        }

    batch.updated_at = now
    _record_action(db, operation_id, "materialize_targets",
                   batch_no=batch.batch_no,
                   details={"count": len(all_ids), "selector_type": selector_type})
    db.commit()

    # 检查是否还有更多
    check_next = _resolve_selector_ids(
        db, selector_payload, cutoff, batch.cursor_start_json, 1,
    )
    if check_next:
        return HandlerResult(
            HandlerOutcome.CONTINUE,
            reason=f"materialized {len(all_ids)} targets, more pending",
        )
    return None


def _resolve_selector_ids(
    db: Session,
    selector_payload: dict[str, Any],
    cutoff: datetime,
    cursor: dict[str, Any] | None,
    limit: int,
) -> list[str]:
    """按 selector_type + 复合游标 keyset 分页加载目标 ID 列表。

    spec P/§10：禁止数据库 offset；统一使用 (created_at, id) 或 (id) keyset
    游标续跑，崩溃后可安全恢复无跳过/重复。
    """
    selector_type = selector_payload.get("selector_type", "")

    # ── bounded explicit IDs：Python 切片（非 DB offset） ──
    if selector_type == "memory_ids":
        ids = selector_payload.get("memory_ids", [])
        offset = cursor.get("offset", 0) if cursor else 0
        return ids[offset : offset + limit]

    if selector_type in ("turn_ids", "event_ids"):
        key = "turn_ids" if selector_type == "turn_ids" else "event_ids"
        ids = selector_payload.get(key, [])
        offset = cursor.get("offset", 0) if cursor else 0
        return ids[offset : offset + limit]

    # ── 宽选择器：按 (created_at, id) keyset 物化 Target ──
    q = db.query(MemoryRecord.id, MemoryRecord.created_at)

    if selector_type == "thread":
        tid = selector_payload.get("thread_id")
        if not tid:
            return []
        q = q.filter(MemoryRecord.scope_type == "thread", MemoryRecord.scope_id == tid)

    elif selector_type == "canonical_key":
        ck = selector_payload.get("canonical_key")
        if not ck:
            return []
        q = q.filter(MemoryRecord.canonical_key == ck)

    elif selector_type == "scope":
        st = selector_payload.get("scope_type")
        si = selector_payload.get("scope_id")
        if not st or not si:
            return []
        q = q.filter(MemoryRecord.scope_type == st, MemoryRecord.scope_id == si)

    elif selector_type == "time_range":
        tf = selector_payload.get("time_from")
        tt = selector_payload.get("time_to")
        # payload 中时间可能为 ISO 字符串（由 selector_normalizer 规范化）
        if isinstance(tf, str):
            try:
                tf_str = tf.replace("Z", "+00:00")
                tf = datetime.fromisoformat(tf_str)
            except (ValueError, TypeError):
                tf = None
        if isinstance(tt, str):
            try:
                tt_str = tt.replace("Z", "+00:00")
                tt = datetime.fromisoformat(tt_str)
            except (ValueError, TypeError):
                tt = None
        if tf:
            q = q.filter(MemoryRecord.created_at >= tf)
        if tt:
            q = q.filter(MemoryRecord.created_at <= tt)

    elif selector_type == "all_user_data":
        # 全部 memory_records created_at <= cutoff
        pass

    else:
        return []

    q = q.filter(MemoryRecord.created_at <= cutoff)
    q = q.order_by(MemoryRecord.created_at.asc(), MemoryRecord.id.asc())

    # keyset 游标续跑（取代 offset）
    if cursor and cursor.get("last_created_at") and cursor.get("last_id"):
        from sqlalchemy import or_, and_
        last_time = datetime.fromisoformat(cursor["last_created_at"])
        last_id_str = cursor["last_id"]
        q = q.filter(
            or_(
                MemoryRecord.created_at > last_time,
                and_(
                    MemoryRecord.created_at == last_time,
                    MemoryRecord.id > last_id_str,
                ),
            )
        )

    rows = q.limit(limit).all()
    return [r[0] for r in rows]


def _ensure_target(
    db: Session, operation_id: str, target_type: str, target_id: str,
    batch_no: int, now: datetime,
) -> None:
    """幂等写入单个 ForgetTarget。"""
    existing = (
        db.query(ForgetTarget)
        .filter_by(
            forget_operation_id=operation_id,
            target_type=target_type,
            target_id=target_id,
        )
        .first()
    )
    if existing:
        return
    target = ForgetTarget(
        id=_generate_id(),
        forget_operation_id=operation_id,
        target_type=target_type,
        target_id=target_id,
        batch_no=batch_no,
        frozen_at=now,
    )
    db.add(target)


# ── Phase 2: Tombstone 写入（修复宽选择器） ──


def _write_tombstones_for_targets(db: Session, operation_id: str) -> None:
    """为所有已物化但尚无 Tombstone 的 Target 创建 Tombstone。

    - memory_record 目标：写 tombstone（回读 MemoryRecord 填充 canonical_key /
      scope / value_fingerprint，保证 block_reingestion 可匹配）+ 置 forgotten +
      删向量投影；
    - event 目标：tombstone 带 source_event_id（与 visibility/reingestion 查询对齐）；
    - turn_record 目标：tombstone 带 source_turn_record_id；
    - 由 event/turn 反查 MemoryEvidence / ContentProvenanceRef 派生 memory_record
      目标一并处理（由被忘 turn/event 抽取出的派生记忆也要遗忘）；
    - 持久化来源 Event Tombstone：被忘 MemoryRecord 的来源 Event、被忘 Turn 下
      的全部子 Event 均写 event Tombstone，作为 Shield 退休后的长期屏蔽依据。

    bounded explicit IDs 已由 Phase A 处理（此处按已有 tombstone 幂等跳过）。
    """
    op = db.query(ForgetOperation).filter_by(id=operation_id).first()
    is_audit_readable = (op.mode == "memory_only") if op else False
    now = _utcnow()

    # 已有 Tombstone 的 (target_type, target_id)（Phase A 处理的 bounded IDs）
    tomb_key_set = {
        (row[0], row[1]) for row in db.query(
            ForgetTombstone.target_type, ForgetTombstone.target_id,
        ).filter(
            ForgetTombstone.forget_operation_id == operation_id,
        ).all()
    }

    targets = (
        db.query(ForgetTarget)
        .filter_by(forget_operation_id=operation_id)
        .all()
    )
    event_target_ids = [t.target_id for t in targets if t.target_type == "event"]
    turn_target_ids = [t.target_id for t in targets if t.target_type == "turn_record"]

    # ── 由 event/turn 派生 memory_record 目标（cascade：派生记忆也要遗忘） ──
    derived_event_ids = list(event_target_ids)
    if turn_target_ids:
        turn_tokens = _normalize_turn_tokens(db, turn_target_ids)
        turn_event_rows = db.query(Event.id).filter(
            Event.turn_id.in_(turn_tokens),
        ).all()
        derived_event_ids.extend(r[0] for r in turn_event_rows)
    if derived_event_ids:
        # MemoryEvidence（purge 前存在）+ ContentProvenanceRef（持久，Evidence
        # 被删除后仍可反查）双来源，保证 Cascade 重跑也能定位派生记忆。
        derived_memory_ids: set[str] = {
            r[0] for r in db.query(MemoryEvidence.memory_id).filter(
                MemoryEvidence.source_event_id.in_(derived_event_ids),
            ).distinct().all() if r[0]
        }
        derived_memory_ids.update(
            r[0] for r in db.query(ContentProvenanceRef.owner_id).filter(
                ContentProvenanceRef.owner_type == "memory_record",
                ContentProvenanceRef.source_type == "event",
                ContentProvenanceRef.source_id.in_(derived_event_ids),
            ).distinct().all() if r[0]
        )
        existing_target_keys = {(t.target_type, t.target_id) for t in targets}
        for mid in sorted(derived_memory_ids):
            if ("memory_record", mid) in existing_target_keys:
                continue
            _ensure_target(db, operation_id, "memory_record", mid, 0, now)
        if derived_memory_ids:
            db.flush()
            targets = (
                db.query(ForgetTarget)
                .filter_by(forget_operation_id=operation_id)
                .all()
            )

    memory_target_ids = [
        t.target_id for t in targets if t.target_type == "memory_record"
    ]

    # ── 来源 Event 派生（Shield 退休后的长期屏蔽依据）──
    #  Cascade 阶段把「被忘 MemoryRecord 的来源 Event」与「被忘 Turn 下的全部
    #  子 Event」一并持久化为 event Tombstone：这些是普通读取路径的长期屏蔽
    #  依据，不依赖 Phase A 的临时 Shield。allow_audit_read 由 Forget Mode 决定。
    source_event_ids: set[str] = set()
    if memory_target_ids:
        for row in db.query(ContentProvenanceRef.source_id).filter(
            ContentProvenanceRef.owner_type == "memory_record",
            ContentProvenanceRef.source_type == "event",
            ContentProvenanceRef.owner_id.in_(memory_target_ids),
        ).all():
            if row[0]:
                source_event_ids.add(row[0])
        for row in db.query(MemoryEvidence.source_event_id).filter(
            MemoryEvidence.memory_id.in_(memory_target_ids),
            MemoryEvidence.source_event_id.isnot(None),
        ).all():
            eid = row[0]
            if eid:
                source_event_ids.add(eid)
    # derived_event_ids 已含 event 目标 + turn 子 Event；并入来源 Event 去重
    event_tombstone_ids = set(derived_event_ids) | source_event_ids

    # ── memory_record tombstone：回读记录填充可匹配字段（#15） ──
    mem_by_id: dict[str, MemoryRecord] = {}
    if memory_target_ids:
        mem_by_id = {
            r.id: r for r in db.query(MemoryRecord).filter(
                MemoryRecord.id.in_(memory_target_ids),
            ).all()
        }
    for mid in memory_target_ids:
        key = ("memory_record", mid)
        if key in tomb_key_set:
            continue
        rec = mem_by_id.get(mid)
        value_fingerprint = None
        fingerprint_key_version = None
        if rec is not None:
            try:
                from aiive.forget.fingerprint import compute_value_fingerprint
                value_fingerprint, fingerprint_key_version = compute_value_fingerprint(
                    f"{rec.canonical_key}|{rec.content or ''}"
                )
            except Exception:
                logger.warning("计算 memory %s 内容指纹失败，跳过指纹写入", mid)
        db.add(ForgetTombstone(
            id=_generate_id(),
            forget_operation_id=operation_id,
            target_type="memory_record",
            target_id=mid,
            canonical_key=rec.canonical_key if rec else None,
            scope_type=rec.scope_type if rec else None,
            scope_id=rec.scope_id if rec else None,
            value_fingerprint=value_fingerprint,
            fingerprint_key_version=fingerprint_key_version,
            block_visibility=True,
            block_reingestion=True,
            content_purged=False,
            allow_audit_read=is_audit_readable,
            reason_code="forget",
            created_at=now,
        ))
        tomb_key_set.add(key)

    # ── event tombstone（source_event_id 与 visibility_service 查询方式对齐） ──
    #  含显式 event 目标、被忘 Turn 下的子 Event、以及被忘 Memory 的来源 Event。
    for eid in sorted(event_tombstone_ids):
        key = ("event", eid)
        if key in tomb_key_set:
            continue
        db.add(ForgetTombstone(
            id=_generate_id(),
            forget_operation_id=operation_id,
            target_type="event",
            target_id=eid,
            source_event_id=eid,
            block_visibility=True,
            block_reingestion=True,
            content_purged=False,
            allow_audit_read=is_audit_readable,
            reason_code="forget",
            created_at=now,
        ))
        tomb_key_set.add(key)

    # ── turn_record tombstone（source_turn_record_id） ──
    for tid in turn_target_ids:
        key = ("turn_record", tid)
        if key in tomb_key_set:
            continue
        db.add(ForgetTombstone(
            id=_generate_id(),
            forget_operation_id=operation_id,
            target_type="turn_record",
            target_id=tid,
            source_turn_record_id=tid,
            block_visibility=True,
            block_reingestion=True,
            content_purged=False,
            allow_audit_read=is_audit_readable,
            reason_code="forget",
            created_at=now,
        ))
        tomb_key_set.add(key)

    if memory_target_ids:
        db.query(MemoryRecord).filter(
            MemoryRecord.id.in_(memory_target_ids)
        ).update({
            MemoryRecord.lifecycle_state: "forgotten",
            MemoryRecord.updated_at: now,
        }, synchronize_session=False)
        db.query(MemoryVectorProjection).filter(
            MemoryVectorProjection.memory_id.in_(memory_target_ids)
        ).delete(synchronize_session=False)
    db.flush()


# ── Phase 3: 发现 Dependency ──


def _get_pending_target_ids(
    db: Session, operation_id: str, batch: ForgetBatch,
) -> list[str]:
    """获取未处理 Dependency 的 target ID 列表。"""
    existing = (
        db.query(ForgetDependency.target_id)
        .filter_by(forget_operation_id=operation_id)
        .distinct()
    )
    rows = (
        db.query(ForgetTarget.id)
        .filter(
            ForgetTarget.forget_operation_id == operation_id,
            ForgetTarget.batch_no == batch.batch_no,
            ForgetTarget.id.notin_(existing),
        )
        .limit(BATCH_SIZE)
        .all()
    )
    return [r.id for r in rows]


def _discover_dependencies(
    db: Session,
    operation_id: str,
    target_ids: list[str],
    batch: ForgetBatch,
) -> HandlerResult | None:
    """从 Target 发现依赖（MemoryEvidence / proposals 等）。"""
    targets = (
        db.query(ForgetTarget)
        .filter(ForgetTarget.id.in_(target_ids))
        .all()
    )
    now = _utcnow()

    discovered = 0
    for target in targets:
        discovered += _discover_for_target(db, operation_id, target, batch, now)

    batch.cursor_start_json = {
        "target": target_ids[-1] if target_ids else "",
    }
    batch.updated_at = now
    db.commit()

    # 检查是否还有未处理的 target
    remaining = _get_pending_target_ids(db, operation_id, batch)
    if remaining:
        return HandlerResult(
            HandlerOutcome.CONTINUE,
            reason=f"discovered {discovered} dependencies, more targets pending",
        )
    return None


def _discover_for_target(
    db: Session,
    operation_id: str,
    target: ForgetTarget,
    batch: ForgetBatch,
    _now: datetime,
) -> int:
    """为单个 Target 发现并写入 Dependency。"""
    count = 0
    target_type = target.target_type
    target_id = target.target_id

    src_event_ids: list[str] = []

    if target_type == "memory_record":
        # MemoryEvidence
        evidence_rows = (
            db.query(MemoryEvidence)
            .filter(MemoryEvidence.memory_id == target_id)
            .all()
        )
        src_event_ids = [ev.source_event_id for ev in evidence_rows if ev.source_event_id]
        for ev in evidence_rows:
            _ensure_dependency(
                db, operation_id, target.id, "evidence", ev.id, batch,
            )
            count += 1
        # MemoryEvidence 可能已被上一次 Forget purge 删除；ContentProvenanceRef
        # 持久，补齐来源 Event，保证后续 Operation 仍能发现受波及的 Summary。
        for row in db.query(ContentProvenanceRef.source_id).filter(
            ContentProvenanceRef.owner_type == "memory_record",
            ContentProvenanceRef.source_type == "event",
            ContentProvenanceRef.owner_id == target_id,
        ).all():
            if row[0] and row[0] not in src_event_ids:
                src_event_ids.append(row[0])
    elif target_type == "event":
        src_event_ids = [target_id]
    elif target_type == "turn_record":
        turn_tokens = _normalize_turn_tokens(db, [target_id])
        src_event_ids = [
            r[0] for r in db.query(Event.id).filter(
                Event.turn_id.in_(turn_tokens),
            ).all()
        ]

    # SegmentSummary / EpochCheckpoint 依赖：被忘目标的来源 event 若出现在
    # 某 Summary 的 source_event_ids 中，该 Summary 受波及需重建（spec K）。
    # 进一步，Summary 所属 Segment → Epoch → EpochCheckpoint，对应 checkpoint
    # 同样需 redacted 重建。
    if src_event_ids:
        affected_segment_ids: set[str] = set()
        for ev_id in src_event_ids:
            summaries = _summaries_containing_event(db, ev_id)
            for summary in summaries:
                _ensure_dependency(
                    db, operation_id, target.id,
                    "segment_summary", summary.id, batch,
                )
                count += 1
                affected_segment_ids.add(summary.segment_id)
        # EpochCheckpoint 依赖：经 Segment → Epoch 反查受波及的 checkpoint
        if affected_segment_ids:
            epoch_ids = {
                row[0] for row in db.query(Segment.epoch_id).filter(
                    Segment.id.in_(affected_segment_ids),
                ).all()
            }
            if epoch_ids:
                checkpoints = db.query(EpochCheckpoint).filter(
                    EpochCheckpoint.epoch_id.in_(epoch_ids),
                ).all()
                for cp in checkpoints:
                    _ensure_dependency(
                        db, operation_id, target.id,
                        "epoch_checkpoint", cp.id, batch,
                    )
                    count += 1

    # Target 自身也作为 dependency 记录（供后续 action 生成使用）
    _ensure_dependency(
        db, operation_id, target.id, target_type, target_id, batch,
    )
    count += 1

    return count


def _ensure_dependency(
    db: Session, operation_id: str, target_id: str,
    dependency_type: str, dependency_id: str, batch: ForgetBatch,
) -> None:
    """幂等写入依赖。"""
    existing = (
        db.query(ForgetDependency)
        .filter_by(
            forget_operation_id=operation_id,
            dependency_type=dependency_type,
            dependency_id=dependency_id,
        )
        .first()
    )
    if existing:
        return
    dep = ForgetDependency(
        id=_generate_id(),
        forget_operation_id=operation_id,
        target_id=target_id,
        dependency_type=dependency_type,
        dependency_id=dependency_id,
        discovery_batch_no=batch.batch_no,
        status="resolved",
        discovered_at=_utcnow(),
    )
    db.add(dep)


# ── Stage 衔接 ──


def _enqueue_next_stage(db: Session, operation_id: str, job_type: str) -> None:
    """为下一个 stage 创建 OutboxJob + ForgetStageRun（幂等）。

    OutboxJob.operation_id 有 UNIQUE 约束：崩溃后重放时若 Job 已存在则复用，
    不再盲插（否则 IntegrityError → 整个 stage 死信）。参考
    reconcile_service._ensure_stage 的写法。
    """
    import uuid
    op_id = f"forget:{operation_id}:{job_type.split('_', 1)[1] if '_' in job_type else job_type}"
    job = db.query(OutboxJob).filter(OutboxJob.operation_id == op_id).first()
    if job is None:
        job = OutboxJob(
            operation_id=op_id,
            job_type=job_type,
            status="pending",
            payload={"forget_operation_id": operation_id},
            max_retries=3,
        )
        db.add(job)
        db.flush()

    stage = job_type.split("forget_", 1)[1] if job_type.startswith("forget_") else job_type
    existing_run = (
        db.query(ForgetStageRun)
        .filter_by(forget_operation_id=operation_id, stage=stage)
        .first()
    )
    if existing_run is not None:
        return
    stage_run = ForgetStageRun(
        id=str(uuid.uuid4()),
        forget_operation_id=operation_id,
        stage=stage,
        outbox_job_id=job.id,
        status="pending",
    )
    db.add(stage_run)


# ═══════════════════════════════════════════════════════════════════
# forget_rebuild_dependencies — Phase C/K/L: Evidence 重算 + Summary/Checkpoint 重建
# ═══════════════════════════════════════════════════════════════════


def handle_forget_rebuild(claimed: ClaimedJob) -> HandlerResult:
    """Phase C/K/L: 重建受影响的派生数据。

    1. Memory Evidence 重算：删除被 forget 的 evidence → 重算 confidence/lifecycle
    2. SegmentSummary 重建：受影响的 Summary 生成 redacted 版本或调用 P3 LLM
    3. EpochCheckpoint 重建：基于新 Summary 聚合

    CONTINUE 分页处理。
    """
    payload = claimed.payload
    operation_id = payload.get("forget_operation_id", "")
    if not operation_id:
        return HandlerResult(HandlerOutcome.NON_RETRYABLE, "missing forget_operation_id")

    db = SessionLocal()
    try:
        op = db.query(ForgetOperation).filter_by(id=operation_id).first()
        if not op:
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "operation not found")

        # 查找或创建 rebuild batch
        batch = _find_or_create_composite_batch(db, operation_id, "rebuild", "evidence")

        # Stage 1: Evidence 重算
        if batch.status == "pending":
            result = _recompute_evidence_batch(db, operation_id, batch)
            if result is not None:
                db.close()
                return result
            batch.status = "running"

        # Stage 2: 受影响 Memory 状态更新
        _update_forgotten_memory_states(db, operation_id)

        # Stage 3: Summary 重建
        summary_batch = _find_or_create_composite_batch(
            db, operation_id, "rebuild", "segment_summary",
        )
        if summary_batch.status == "pending":
            result = _rebuild_summaries_batch(db, operation_id, summary_batch)
            if result is not None:
                db.close()
                return result
            summary_batch.status = "done"

        # Stage 4: EpochCheckpoint 重建
        ckpt_batch = _find_or_create_composite_batch(
            db, operation_id, "rebuild", "epoch_checkpoint",
        )
        if ckpt_batch.status == "pending":
            _rebuild_checkpoints(db, operation_id, ckpt_batch)
            ckpt_batch.status = "done"

        # 全部完成
        batch.status = "done"
        batch.updated_at = _utcnow()
        _enqueue_next_stage(db, operation_id, "forget_purge")
        db.commit()

        return HandlerResult(HandlerOutcome.COMPLETED, "rebuild_completed")
    except Exception as exc:
        db.rollback()
        logger.exception("forget_rebuild failed")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"rebuild: {exc}")
    finally:
        db.close()


def _find_or_create_composite_batch(
    db: Session, operation_id: str, stage: str, dependency_type: str,
) -> ForgetBatch:
    """查找或创建指定 stage + dependency_type 的 batch。"""
    batch = (
        db.query(ForgetBatch)
        .filter_by(
            forget_operation_id=operation_id,
            stage=stage,
            dependency_type=dependency_type,
        )
        .first()
    )
    if batch:
        return batch
    batch = ForgetBatch(
        id=_generate_id(),
        forget_operation_id=operation_id,
        stage=stage,
        dependency_type=dependency_type,
        batch_no=0,
        cursor_lane="id",
        status="pending",
    )
    db.add(batch)
    db.flush()
    return batch


def _recompute_evidence_batch(
    db: Session, operation_id: str, batch: ForgetBatch,
) -> HandlerResult | None:
    """重算 Memory Evidence：仅删除被忘来源（tombstone 拦截）的证据，重算置信度。

    保留仍有其他独立证据（非被忘来源）的 memory；仅当所有证据均来自被忘来源、
    独立证据归零时，才将该 memory 置为 forgotten（spec L）。

    使用 keyset 游标（target_id）续跑，禁止 offset。
    """
    cursor = batch.cursor_start_json
    q = db.query(ForgetTarget).filter_by(
        forget_operation_id=operation_id, target_type="memory_record",
    ).order_by(ForgetTarget.target_id.asc())

    if cursor and cursor.get("last_id"):
        q = q.filter(ForgetTarget.target_id > cursor["last_id"])

    targets = q.limit(BATCH_SIZE).all()
    if not targets:
        return None

    # 本 operation 下被 tombstone 拦截的来源 event（被忘来源）
    blocked_source_events = {
        row[0] for row in db.query(ForgetTombstone.source_event_id).filter(
            ForgetTombstone.forget_operation_id == operation_id,
            ForgetTombstone.source_event_id.isnot(None),
        ).all()
    }

    for target in targets:
        mid = target.target_id
        # 仅删除来自被忘来源的证据，保留独立证据
        if blocked_source_events:
            db.query(MemoryEvidence).filter(
                MemoryEvidence.memory_id == mid,
                MemoryEvidence.source_event_id.in_(list(blocked_source_events)),
            ).delete(synchronize_session=False)

        # 重算剩余独立证据数
        remaining = (
            db.query(MemoryEvidence)
            .filter(MemoryEvidence.memory_id == mid)
            .count()
        )
        rec = db.query(MemoryRecord).filter(MemoryRecord.id == mid).first()
        if rec is None:
            continue
        if remaining == 0:
            # 无独立证据 → forgotten
            rec.lifecycle_state = "forgotten"
            rec.updated_at = _utcnow()
            db.query(MemoryVectorProjection).filter_by(memory_id=mid).delete()
        else:
            # 设计确认（#27，保守/隐私优先）：在完整 Saga 内，所有 ForgetTarget
            # 已在 cascade 阶段被无条件置为 forgotten（_write_tombstones_for_targets），
            # 因此本分支不会「保留」任何被明确选中遗忘的 memory——它只重算置信度，
            # 不会也不得把 lifecycle 改回 active。保留此分支仅用于证据簿记
            # （剩余独立证据数的可观测性），维持 cascade 全量遗忘语义。
            rec.confidence = min(1.0, 0.5 + 0.05 * remaining)
            rec.updated_at = _utcnow()

    batch.cursor_start_json = {"last_id": targets[-1].target_id}
    batch.updated_at = _utcnow()
    _record_action(db, operation_id, "recompute_evidence",
                   batch_no=batch.batch_no,
                   details={"count": len(targets)})
    db.commit()

    if len(targets) == BATCH_SIZE:
        return HandlerResult(
            HandlerOutcome.CONTINUE,
            reason=f"recomputed evidence for {len(targets)} memories, more pending",
        )
    return None


def _update_forgotten_memory_states(db: Session, operation_id: str) -> None:
    """将所有无 evidence 的 target memory 置为 forgotten。"""
    targets = (
        db.query(ForgetTarget)
        .filter_by(forget_operation_id=operation_id, target_type="memory_record")
        .all()
    )
    for target in targets:
        remaining = (
            db.query(MemoryEvidence)
            .filter(
                MemoryEvidence.memory_id == target.target_id,
                MemoryEvidence.source_event_id.isnot(None),
            )
            .count()
        )
        if remaining == 0:
            db.query(MemoryRecord).filter(
                MemoryRecord.id == target.target_id
            ).update({
                MemoryRecord.lifecycle_state: "forgotten",
                MemoryRecord.updated_at: _utcnow(),
            }, synchronize_session=False)
            db.query(MemoryVectorProjection).filter_by(
                memory_id=target.target_id,
            ).delete()
    db.flush()


def _rebuild_summaries_batch(
    db: Session, operation_id: str, batch: ForgetBatch,
) -> HandlerResult | None:
    """重建受影响的 SegmentSummary。

    - 有剩余 source Turn → 尝试调用 P3 LLM 生成新 Summary
    - 无剩余 Turn → 生成确定性 empty/redacted Summary
    - 原子切换：新 version → Segment.summary_id → 旧版索引 tombstone → 同事务 commit
    - enqueue retrieval_index_refresh（非同步刷新）
    """
    from sqlalchemy import func as sa_func

    cursor = batch.cursor_start_json

    # 加载受影响 Summary 依赖（keyset 游标）
    q = db.query(ForgetDependency).filter_by(
        forget_operation_id=operation_id,
        dependency_type="segment_summary",
    ).order_by(ForgetDependency.dependency_id.asc())
    if cursor and cursor.get("last_id"):
        q = q.filter(ForgetDependency.dependency_id > cursor["last_id"])
    deps = q.limit(BATCH_SIZE).all()

    if not deps:
        return None

    redacted_text = "[redacted: all source forgotten]"
    now = _utcnow()
    import uuid as _uuid

    def _as_list(value: Any) -> list[Any]:
        """兼容 payload 中的 JSON 字符串 / list，统一为 list 写入 JSON 列。"""
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            import json
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except (ValueError, TypeError):
                return []
        return []

    for dep in deps:
        summary = db.query(SegmentSummary).filter(
            SegmentSummary.id == dep.dependency_id
        ).first()
        if summary is None:
            continue
        segment_id = summary.segment_id
        if not segment_id:
            continue

        # 尝试 P3 LLM 生成
        summary_payload = _try_generate_summary(db, segment_id, operation_id)
        if summary_payload is None:
            # 无剩余 source → 用 redacted
            summary_payload = {
                "goal": redacted_text, "outcome": redacted_text,
                "decisions": [], "open_loops": [], "entities": [],
                "active_constraints": [], "unresolved_failures": [],
                "important_tool_results": [], "omitted_artifact_refs": [],
                "source_turn_ids": [], "source_event_ids": [],
                "source_hash": "",
                "model_id": "redacted", "token_count": 0,
            }

        # 新版本号：该 segment 当前 max(summary_version)+1（避免撞
        # uq_segment_summary_segment_version；二次遗忘也安全）
        max_ver_row = db.query(
            sa_func.max(SegmentSummary.summary_version),
        ).filter(SegmentSummary.segment_id == segment_id).first()
        new_version = int(max_ver_row[0] or 0) + 1 if max_ver_row else 1

        src_ids = [str(x) for x in _as_list(summary_payload.get("source_event_ids")) if x]

        # 写新 Summary（ORM，补齐 NOT NULL 的 token_count / model_id 等必填列；
        # segment_summaries 无 is_searchable 列，旧版隐藏语义由下方索引 tombstone 实现）
        new_id = str(_uuid.uuid4())
        db.add(SegmentSummary(
            id=new_id,
            segment_id=segment_id,
            goal=summary_payload.get("goal") or "",
            outcome=summary_payload.get("outcome") or "",
            decisions=_as_list(summary_payload.get("decisions")),
            open_loops=_as_list(summary_payload.get("open_loops")),
            entities=_as_list(summary_payload.get("entities")),
            active_constraints=_as_list(summary_payload.get("active_constraints")),
            unresolved_failures=_as_list(summary_payload.get("unresolved_failures")),
            important_tool_results=_as_list(summary_payload.get("important_tool_results")),
            omitted_artifact_refs=_as_list(summary_payload.get("omitted_artifact_refs")),
            source_turn_ids=_as_list(summary_payload.get("source_turn_ids")),
            source_event_ids=src_ids,
            source_hash=summary_payload.get("source_hash") or "",
            summary_version=new_version,
            model_id=summary_payload.get("model_id") or "redacted",
            token_count=_non_negative_int(summary_payload.get("token_count")),
            created_at=now,
        ))
        db.flush()

        # 原子更新 segment.summary_id
        db.query(Segment).filter(Segment.id == segment_id).update(
            {Segment.summary_id: new_id}, synchronize_session=False,
        )

        # 旧版 Summary 的检索索引条目 tombstone（#7：旧版不可再被检索）
        _enqueue_index_tombstone(
            db, "segment_summary", dep.dependency_id, "forget_tombstone",
        )

        # 写入 ContentProvenanceRef（spec N）：记录新 Summary 及其来源 Event，
        # 供 ForgetVerifier 按 source_type+source_id 反查所有副本位置
        for src_ev_id in src_ids:
            existing_prov = db.query(ContentProvenanceRef).filter_by(
                owner_type="segment_summary",
                owner_id=new_id,
                source_type="event",
                source_id=src_ev_id,
            ).first()
            if existing_prov is None:
                db.add(ContentProvenanceRef(
                    id=_generate_id(),
                    owner_type="segment_summary",
                    owner_id=new_id,
                    source_type="event",
                    source_id=src_ev_id,
                ))

        # enqueue retrieval_index_refresh（同一事务，非同步刷新）
        refresh_op_id = f"retrieval_refresh:segment_summary:{new_id}:{new_version}"
        existing_refresh = db.query(OutboxJob).filter(
            OutboxJob.operation_id == refresh_op_id,
        ).first()
        if existing_refresh is None:
            db.add(OutboxJob(
                operation_id=refresh_op_id,
                job_type="retrieval_index_refresh",
                status="pending",
                payload={
                    "schema_version": 1,
                    "source_type": "segment_summary",
                    "source_id": new_id,
                    "source_version": new_version,
                    "event_type": "segment.forget_rebuilt",
                },
                max_retries=3,
            ))

    batch.cursor_start_json = {"last_id": deps[-1].dependency_id}
    batch.updated_at = now
    _record_action(db, operation_id, "rebuild_summaries",
                   batch_no=batch.batch_no,
                   details={"count": len(deps)})
    db.commit()

    if len(deps) == BATCH_SIZE:
        return HandlerResult(
            HandlerOutcome.CONTINUE,
            reason=f"rebuilt {len(deps)} summaries, more pending",
        )
    return None


def _rebuild_checkpoints(
    db: Session, operation_id: str, _batch: ForgetBatch,
) -> None:
    """重建受影响的 EpochCheckpoint——生成 redacted 版本并原子切换 epoch.checkpoint_id。

    - 旧 checkpoint 内容就地 redact，且其检索索引条目入队 tombstone（#7：
      索引 snippet 里的旧正文不得继续可检索）；
    - 新版本号取该 epoch 当前 max(version)+1（#14：二次遗忘不撞
      uq_epoch_checkpoint_epoch_version）。
    """
    from sqlalchemy import func as sa_func
    import uuid as _uuid
    now = _utcnow()

    deps = (
        db.query(ForgetDependency)
        .filter_by(forget_operation_id=operation_id, dependency_type="epoch_checkpoint")
        .all()
    )
    for dep in deps:
        cp = db.query(EpochCheckpoint).filter(
            EpochCheckpoint.id == dep.dependency_id,
        ).first()
        if cp is None:
            continue
        epoch_id = cp.epoch_id

        # 旧 checkpoint 就地 redact（ORM）
        cp.current_goal = "[redacted]"
        cp.open_loops = []
        cp.active_constraints = []
        cp.current_decisions = []
        cp.completed_milestones = []
        cp.referenced_artifacts = []
        cp.relevant_entities = []
        cp.latest_verified_tool_states = []

        # 旧 checkpoint 的索引条目 tombstone（#7）
        _enqueue_index_tombstone(
            db, "epoch_checkpoint", cp.id, "forget_tombstone",
        )

        # 幂等：epoch.checkpoint_id 已指向 redacted 新版则不再重复插入
        ep = db.query(Epoch).filter(Epoch.id == epoch_id).first()
        if ep is not None and ep.checkpoint_id and ep.checkpoint_id != cp.id:
            cur = db.query(EpochCheckpoint).filter(
                EpochCheckpoint.id == ep.checkpoint_id,
            ).first()
            if cur is not None and cur.current_goal == "[redacted]":
                continue

        max_ver_row = db.query(
            sa_func.max(EpochCheckpoint.version),
        ).filter(EpochCheckpoint.epoch_id == epoch_id).first()
        new_version = int(max_ver_row[0] or 0) + 1 if max_ver_row else 1
        new_cp = EpochCheckpoint(
            id=str(_uuid.uuid4()),
            epoch_id=epoch_id,
            version=new_version,
            current_goal="[redacted]",
            open_loops=[],
            active_constraints=[],
            current_decisions=[],
            source_segment_ids=[],
            source_hashes=[],
            token_count=0,
            created_at=now,
        )
        db.add(new_cp)
        db.flush()

        db.query(Epoch).filter(Epoch.id == epoch_id).update(
            {Epoch.checkpoint_id: new_cp.id}, synchronize_session=False,
        )
    db.flush()


def _try_generate_summary(
    db: Session, segment_id: str, operation_id: str,
) -> dict[str, Any] | None:
    """尝试通过 P3 LLM 为剩余 source Turn 生成新 Summary。

    加载 Segment 的剩余 Event/Turn（未被 forget），调用 P3 builder 生成。
    若无剩余 source → 返回 None，由调用方使用 redacted。
    """
    from aiive.core.llm_client import LLMClient
    from aiive.runtime.compaction import count_summary_tokens, merge_summary
    from aiive.worker.outbox_handlers import _build_summary_prompt, _extract_json  # pyright: ignore[reportPrivateUsage]

    import hashlib

    from aiive.runtime.compaction import event_content_hash

    # 加载 segment
    segment = db.query(Segment).filter(Segment.id == segment_id).first()
    if not segment:
        return None

    from aiive.forget.visibility_service import ForgetVisibilityService

    logger.debug(
        "summary rebuild for operation %s segment %s", operation_id, segment_id,
    )
    # 经 TurnRecord.segment_id 关联取「该 segment」的事件（Event 无 segment_id），
    # 按 (turn_sequence, turn_event_index) 确定性排序（参考 raw_history_expander）。
    # 统一走 ForgetVisibilityService：覆盖所有有效 Event Tombstone（不限当前
    # Operation）、TurnRecord Tombstone 覆盖的子 Event、以及作为被遗忘
    # MemoryRecord 来源的 Event。过滤必须先于最终 50 条上限，否则被屏蔽内容
    # 会残留进 LLM 输入与新 Summary 的 source_* 字段。
    events = (
        db.query(Event, TurnRecord.turn_id)
        .join(TurnRecord, TurnRecord.turn_id == Event.turn_id)
        .filter(TurnRecord.segment_id == segment_id)
        .order_by(TurnRecord.turn_sequence.asc(), Event.turn_event_index.asc())
        .all()
    )
    if not events:
        return None

    blocked_ids = ForgetVisibilityService.blocked_target_ids(
        db, "event", [(e.id, e.created_at) for e, _ in events],
    )
    remaining: list[tuple[Event, str]] = [
        (e, tid) for e, tid in events if e.id not in blocked_ids
    ][:50]
    source_turns = [
        {"role": e.event_type, "content": str((e.payload or {}).get("content", ""))[:500]}
        for e, _ in remaining
    ]

    if not source_turns:
        return None  # 无剩余 source → redacted

    source_event_ids = [e.id for e, _ in remaining]
    source_turn_ids = sorted({tid for _, tid in remaining if tid})
    # 真实 source_hash：基于剩余事件的 content hash 序列（确定性）
    source_hash = hashlib.sha256(
        "|".join(
            f"{e.id}:{event_content_hash(e)}" for e, _ in remaining
        ).encode("utf-8")
    ).hexdigest()

    # 调用 P3 LLM
    try:
        llm = LLMClient(
            base_url=settings.aiive_llm_base_url,
            api_key=settings.aiive_llm_api_key,
            default_model=settings.aiive_llm_model,
            timeout_seconds=settings.aiive_llm_timeout_seconds,
        )
        messages = _build_summary_prompt(source_turns, [], [])
        resp = llm.chat(
            messages,
            model=settings.aiive_llm_model,
            temperature=0,
            json_mode=True,
        )
        raw = getattr(resp, "content", None) or getattr(resp, "text", "") or str(resp)
        llm_output = _extract_json(raw)

        summary_text = (llm_output.get("goal", "") + llm_output.get("outcome", ""))
        token_count = count_summary_tokens(settings.aiive_llm_model, summary_text)

        return merge_summary(
            llm_output,
            boundary_snapshot={},
            verified_tool_states=[],
            unresolved_failures=[],
            item_ref_map={"tool_items": [], "failure_items": [], "ref_to_tool_call_id": {}},
            source_turn_ids=source_turn_ids,
            source_event_ids=source_event_ids,
            source_hash=source_hash,
            summary_version=2,
            model_id=settings.aiive_llm_model,
            token_count=token_count,
        )
    except Exception:
        logger.exception("P3 LLM summary rebuild failed for segment %s", segment_id)
        return None  # LLM 失败 → fallback 到 redacted


# ═══════════════════════════════════════════════════════════════════
# forget_purge — Phase E/M: 内容 scrub + 物理删除
# ═══════════════════════════════════════════════════════════════════


def handle_forget_purge(claimed: ClaimedJob) -> HandlerResult:
    """Phase E/M: scrub 所有内容承载位置的用户正文，安全物理删除。

    Scrub 表: memory_records.content, memory_proposals.*, memory_evidence.content_span,
              memory_maintenance_inputs.snapshot_json, retrieval_index_entries.search_text
    物理删除: 仅 safety-checked 行（无 FK/manifest/排序依赖）
    """
    payload = claimed.payload
    operation_id = payload.get("forget_operation_id", "")
    if not operation_id:
        return HandlerResult(HandlerOutcome.NON_RETRYABLE, "missing forget_operation_id")

    db = SessionLocal()
    try:
        op = db.query(ForgetOperation).filter_by(id=operation_id).first()
        mode = op.mode if op is not None else "everywhere"
        batch = _find_or_create_composite_batch(db, operation_id, "purge", "scrub")
        cursor = batch.cursor_start_json

        # 获取 memory_record targets（keyset 游标）
        q = db.query(ForgetTarget).filter_by(
            forget_operation_id=operation_id, target_type="memory_record",
        ).order_by(ForgetTarget.target_id.asc())
        if cursor and cursor.get("last_id"):
            q = q.filter(ForgetTarget.target_id > cursor["last_id"])
        targets = q.limit(BATCH_SIZE).all()

        if targets:
            target_ids = [t.target_id for t in targets]
            _scrub_batch(db, target_ids, mode=mode)
        else:
            # 额外清理旧 evidence / proposals
            target_ids = []

        if targets:
            batch.cursor_start_json = {"last_id": targets[-1].target_id}
        batch.updated_at = _utcnow()
        _record_action(db, operation_id, "scrub",
                       batch_no=batch.batch_no,
                       details={"count": len(targets) if targets else 0})
        db.commit()

        if targets and len(targets) == BATCH_SIZE:
            return HandlerResult(
                HandlerOutcome.CONTINUE,
                reason=f"scrubbed {len(targets)} targets, more pending",
            )

        # event / turn_record 类型目标的正文 scrub（#16：mode=everywhere/history_only
        # 时按 tombstone 的 source_event_id / source_turn_record_id 清除原文）
        _scrub_event_turn_targets(db, operation_id)

        # 范围化 scrub（含 Task Runtime / Knowledge / 对象引用登记）。
        scrub_counts = _scrub_wide(db, operation_id)
        _record_scrub_content_action(db, operation_id, scrub_counts)
        # 先提交数据库清理与 pending delete_object 动作：对象删除必须在提交之后，
        # 否则回滚会留下「数据库有引用但对象已删除」的不可逆状态。
        db.commit()

        # 提交后执行对象删除；失败保留 failed 动作并由本 Job 重试。
        from aiive.forget.content_scrub import process_object_deletions
        object_result = process_object_deletions(db, operation_id)
        if object_result["failed"]:
            logger.warning(
                "forget_purge 对象删除未完成，等待重试：%s", object_result["failed"][:5],
            )
            return HandlerResult(
                HandlerOutcome.RETRYABLE_ERROR,
                f"object deletion failed: {len(object_result['failed'])}",
            )

        # 全部完成 → enqueue verify（同事务）
        batch.status = "done"
        _enqueue_next_stage(db, operation_id, "forget_verify")
        db.commit()
        return HandlerResult(HandlerOutcome.COMPLETED, "purge_completed")
    except Exception as exc:
        db.rollback()
        logger.exception("forget_purge failed")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"purge: {exc}")
    finally:
        db.close()


def _scrub_event_turn_targets(db: Session, operation_id: str) -> None:
    """按 tombstone 的 source_event_id / source_turn_record_id scrub 原文（#16）。

    mode = everywhere / history_only 时清除 events.payload 与
    turn_records.response_payload，并把对应 tombstone 置 content_purged=True；
    memory_only 保留原始历史（allow_audit_read 语义）。
    """
    from sqlalchemy import bindparam, text as sa_text

    op = db.query(ForgetOperation).filter_by(id=operation_id).first()
    if op is None or op.mode == "memory_only":
        return
    now = _utcnow()

    tombs = (
        db.query(ForgetTombstone)
        .filter(
            ForgetTombstone.forget_operation_id == operation_id,
            ForgetTombstone.target_type.in_(["event", "turn_record"]),
            ForgetTombstone.content_purged.is_(False),
        )
        .all()
    )
    event_ids = [
        t.source_event_id or t.target_id for t in tombs if t.target_type == "event"
    ]
    turn_ids = [
        t.source_turn_record_id or t.target_id
        for t in tombs if t.target_type == "turn_record"
    ]

    ids_bind = bindparam("ids", expanding=True)
    if event_ids:
        db.execute(sa_text(
            "UPDATE events SET payload = '{}' WHERE id IN :ids"
        ).bindparams(ids_bind), {"ids": event_ids})
    turn_tokens = _normalize_turn_tokens(db, turn_ids) if turn_ids else set()
    if turn_tokens:
        token_list = sorted(turn_tokens)
        # 同时兼容 TurnRecord.id 与逻辑 turn_id：Turn 行两种列都匹配。
        db.execute(sa_text(
            "UPDATE turn_records SET response_payload = NULL "
            + "WHERE turn_id IN :ids OR id IN :ids"
        ).bindparams(ids_bind), {"ids": token_list})
        # 该 Turn 下全部子 Event 的正文一并清除（Event.turn_id 只匹配逻辑 id）。
        db.execute(sa_text(
            "UPDATE events SET payload = '{}' WHERE turn_id IN :ids"
        ).bindparams(ids_bind), {"ids": token_list})

    # content_purged 必须以「实际完成内容清理」为依据：仅当 Tombstone 覆盖的
    # Event / Turn 已不存在非空正文时才置 True，否则保持 False（fail-closed）。
    def _turn_fully_cleared(tid: str) -> bool:
        toks = _normalize_turn_tokens(db, [tid])
        if not toks:
            return True
        tok_list = sorted(toks)
        from sqlalchemy import or_ as _or
        # 用列查询直接读库，避免 raw UPDATE 后 ORM identity map 返回旧值。
        tr_payloads = [
            r[0] for r in db.query(TurnRecord.response_payload).filter(
                _or(TurnRecord.id.in_(tok_list), TurnRecord.turn_id.in_(tok_list))
            ).all()
        ]
        if any((p or {}) for p in tr_payloads):
            return False
        ev_payloads = [
            r[0] for r in db.query(Event.payload).filter(
                Event.turn_id.in_(tok_list)
            ).all()
        ]
        return all(not (p or {}) for p in ev_payloads)

    for t in tombs:
        if t.target_type == "event":
            eid = t.source_event_id or t.target_id
            row = db.query(Event.payload).filter(Event.id == eid).first() if eid else None
            done = row is None or not (row[0] or {})
        else:
            tid = t.source_turn_record_id or t.target_id
            done = _turn_fully_cleared(tid) if tid else True
        if done:
            t.content_purged = True
            t.purged_at = now
    db.flush()


def _scrub_batch(db: Session, memory_ids: list[str], mode: str = "everywhere") -> None:
    """批量 scrub 内容——覆盖规格 §M 第 0 节清单所有内容承载位置。

    ids 使用 SQLAlchemy expanding 绑定参数，SQLite/PostgreSQL 均兼容
    （SQLite 渲染 IN (:1,:2,...)，PostgreSQL 渲染 = ANY(:ids)）。

    memory_only：保留来源 Event 的原始 payload（含独占来源 Event），仅供显式
    历史审计；其正常可见性屏蔽与防重抽由 Shield / Tombstone / Provenance
    读取侧规则保证，不依赖清空正文。history_only / everywhere 继续执行不可逆
    正文清理。
    """
    from sqlalchemy import bindparam, text as sa_text
    now = _utcnow()
    ids_bind = bindparam("ids", expanding=True)

    # --- 前置：计算专属来源 event（必须在 DELETE evidence 之前）---
    # 共享来源 event 不在窄选择器内 scrub，避免破坏其他仍有效的 memory。
    # source_event_id 列可空：SQL 侧已过滤 NULL，这里用显式循环再收窄一次，保证 list[str]
    event_ids: list[str] = []
    for evidence_row in db.query(MemoryEvidence.source_event_id).filter(
        MemoryEvidence.memory_id.in_(memory_ids),
        MemoryEvidence.source_event_id.isnot(None),
    ).distinct().all():
        source_event_id = evidence_row[0]
        if source_event_id is not None:
            event_ids.append(source_event_id)
    exclusive_event_ids: list[str] = []
    if event_ids:
        surviving = {
            row[0] for row in db.query(MemoryEvidence.source_event_id).filter(
                MemoryEvidence.memory_id.notin_(memory_ids),
                MemoryEvidence.source_event_id.in_(event_ids),
            ).distinct().all()
        }
        exclusive_event_ids = [eid for eid in event_ids if eid not in surviving]

    # --- memory layer ---
    db.execute(sa_text(
        "UPDATE memory_records SET content = '[forgotten]', structured_value = NULL, "
        + "graph_facts = NULL, updated_at = :now WHERE id IN :ids"
    ).bindparams(ids_bind), {"now": now, "ids": memory_ids})
    # --- 时序知识图谱：关系行必须物理删除（隐私承诺），实体锚点随后清理 ---
    graph_entity_ids = [
        row[0] for row in db.execute(sa_text(
            "SELECT DISTINCT subject_entity_id FROM graph_relations "
            + "WHERE source_memory_id IN :ids "
            + "UNION SELECT DISTINCT object_entity_id FROM graph_relations "
            + "WHERE source_memory_id IN :ids"
        ).bindparams(ids_bind), {"ids": memory_ids}).all()
    ]
    db.execute(sa_text(
        "DELETE FROM graph_relations WHERE source_memory_id IN :ids"
    ).bindparams(ids_bind), {"ids": memory_ids})
    for entity_id in graph_entity_ids:
        still_used = db.execute(sa_text(
            "SELECT 1 FROM graph_relations WHERE subject_entity_id = :eid "
            + "OR object_entity_id = :eid LIMIT 1"
        ), {"eid": entity_id}).first()
        if still_used is None:
            db.execute(sa_text("DELETE FROM graph_entities WHERE id = :eid"), {"eid": entity_id})
    # --- 提案：不能只依赖 final_memory_id；按来源 Event / Turn 反查 ---
    from aiive.forget.content_scope import (
        normalize_turn_tokens,
        select_json_array_members,
    )
    proposal_ids: set[str] = {
        row[0] for row in db.query(MemoryProposalModel.id).filter(
            MemoryProposalModel.final_memory_id.in_(memory_ids),
        ).all()
    }
    if event_ids:
        proposal_ids |= set(select_json_array_members(
            db, "memory_proposals", "id", "source_event_ids", event_ids,
        ))
    if event_ids:
        turn_tokens = normalize_turn_tokens(db, [
            str(row[0]) for row in db.query(Event.turn_id).filter(
                Event.id.in_(event_ids), Event.turn_id.isnot(None),
            ).distinct().all() if row[0] is not None
        ])
        if turn_tokens:
            proposal_ids |= {
                row[0] for row in db.query(MemoryProposalModel.id).filter(
                    (MemoryProposalModel.source_turn_id.in_(turn_tokens))
                    | (MemoryProposalModel.source_turn_record_id.in_(turn_tokens))
                ).all()
            }
    if proposal_ids:
        db.execute(sa_text(
            "UPDATE memory_proposals SET content = NULL, structured_value = NULL, "
            + "evidence = '[]', raw_payload = NULL, normalized_payload = NULL "
            + "WHERE id IN :ids"
        ).bindparams(ids_bind), {"ids": sorted(proposal_ids)})
    db.execute(sa_text(
        "DELETE FROM memory_evidence WHERE memory_id IN :ids"
    ).bindparams(ids_bind), {"ids": memory_ids})
    # 其他仍然有效的 memory 若以被清理 Event 为证据：清除 span，保留行供重算。
    if event_ids:
        db.execute(sa_text(
            "UPDATE memory_evidence SET content_span = '' "
            + "WHERE source_event_id IN :ids AND content_span IS NOT NULL"
        ).bindparams(ids_bind), {"ids": event_ids})
    db.execute(sa_text(
        "UPDATE memory_maintenance_actions SET details = NULL, preconditions = NULL "
        + "WHERE subject_memory_record_id IN :ids"
    ).bindparams(ids_bind), {"ids": memory_ids})
    related_action_ids: set[str] = set()
    if memory_ids:
        related_action_ids = set(select_json_array_members(
            db, "memory_maintenance_actions", "id", "related_record_ids", memory_ids,
        ))
        if related_action_ids:
            db.execute(sa_text(
                "UPDATE memory_maintenance_actions SET details = NULL, preconditions = NULL "
                + "WHERE id IN :ids"
            ).bindparams(ids_bind), {"ids": sorted(related_action_ids)})
        block_ids = set(select_json_array_members(
            db, "core_memory_blocks", "id", "source_memory_ids", memory_ids,
        ))
        if block_ids:
            db.execute(sa_text(
                "DELETE FROM core_memory_blocks WHERE id IN :ids"
            ).bindparams(ids_bind), {"ids": sorted(block_ids)})
    db.execute(sa_text(
        "UPDATE memory_maintenance_inputs SET snapshot_json = NULL "
        + "WHERE memory_record_id IN :ids"
    ).bindparams(ids_bind), {"ids": memory_ids})

    # --- retrieval index ---
    db.execute(sa_text(
        "UPDATE retrieval_index_entries SET search_text = '', snippet = '', "
        + "title = '[forgotten]', metadata = '{}', is_searchable = false "
        + "WHERE source_type = 'memory_record' AND source_id IN :ids"
    ).bindparams(ids_bind), {"ids": memory_ids})

    # --- 来源 Event scrub（memory_only 保留原始正文）---
    if mode != "memory_only" and exclusive_event_ids:
        db.execute(sa_text(
            "UPDATE events SET payload = '{}' WHERE id IN :ids"
        ).bindparams(ids_bind), {"ids": exclusive_event_ids})


def _scrub_wide(db: Session, operation_id: str) -> dict[str, int]:
    """宽 / 窄选择器的统一内容清理入口。

    范围由 SelectorManifest 解析为 ContentScope（表/字段/关联均来自真实模型），
    逐表 redact；对象引用解除后登记 pending delete_object 动作，提交后删除。
    memory_only 保留 Event/Turn 原始正文；memory_ids / canonical_key / scope 的
    历史正文只由 _scrub_batch 按「独占来源 Event」处理，不在此扩大范围。
    """
    from aiive.forget.content_scope import load_scope
    from aiive.forget.content_scrub import scrub_all

    op = db.query(ForgetOperation).filter_by(id=operation_id).first()
    if op is None:
        return {}
    scope = load_scope(db, operation_id, op.mode)
    if scope is None:
        return {}

    # 目标 memory 的窄层清理（记录/证据/提案/维护/图谱/检索）。幂等：Purge 已按
    # 批次处理过，这里保证直接调用 _scrub_wide（canonical_key/scope 等）也完整。
    for start in range(0, len(scope.memory_ids), BATCH_SIZE):
        chunk = sorted(scope.memory_ids)[start:start + BATCH_SIZE]
        if chunk:
            _scrub_batch(db, chunk, mode=op.mode)

    # 沿 Provenance / Evidence 把「被清理历史」派生的 memory 一并纳入窄层清理。
    if scope.selector_type not in ("memory_ids", "canonical_key", "scope") and scope.event_ids:
        from sqlalchemy import bindparam, text as sa_text
        ids = sorted(scope.event_ids)
        bind = bindparam("ids", expanding=True)
        derived: set[str] = set()
        for start in range(0, len(ids), BATCH_SIZE):
            chunk = ids[start:start + BATCH_SIZE]
            derived |= {r[0] for r in db.execute(sa_text(
                "SELECT DISTINCT owner_id FROM content_provenance_refs WHERE owner_type = 'memory_record' AND source_type = 'event' AND source_id IN :ids"
            ).bindparams(bind), {"ids": chunk}).all() if r[0]}
            derived |= {r[0] for r in db.execute(sa_text(
                "SELECT DISTINCT memory_id FROM memory_evidence WHERE source_event_id IN :ids"
            ).bindparams(bind), {"ids": chunk}).all() if r[0]}
            derived |= {r[0] for r in db.execute(sa_text(
                "SELECT id FROM memory_records WHERE source_event_id IN :ids"
            ).bindparams(bind), {"ids": chunk}).all() if r[0]}
        extra = sorted(derived - scope.memory_ids)
        for start in range(0, len(extra), BATCH_SIZE):
            _scrub_batch(db, extra[start:start + BATCH_SIZE], mode=op.mode)
        scope.memory_ids |= set(extra)

    counts = scrub_all(db, scope)
    db.flush()
    return counts


# ═══════════════════════════════════════════════════════════════════
# forget_verify — Phase N: ForgetVerifier
# ═══════════════════════════════════════════════════════════════════


def _has_persistent_source_event_protection(db: Session, event_id: str) -> bool:
    """来源 Event 是否具备不依赖 active Shield 的长期保护。

    覆盖三种持久化 Tombstone：
      - 直接 event Tombstone（target_id / source_event_id 命中）；
      - 覆盖该 Event 的 turn_record Tombstone（TurnRecord.id / turn_id 任一语义）；
      - 该 Event 作为来源的 memory_record Tombstone（经 ContentProvenanceRef）。
    三者均须同时 block_visibility=True 与 block_reingestion=True。
    """
    from sqlalchemy import or_

    from aiive.forget.visibility_service import ForgetVisibilityService

    flags = (
        ForgetTombstone.block_visibility.is_(True),
        ForgetTombstone.block_reingestion.is_(True),
    )
    direct = db.query(ForgetTombstone.id).filter(
        ForgetTombstone.target_type == "event",
        or_(
            ForgetTombstone.target_id == event_id,
            ForgetTombstone.source_event_id == event_id,
        ),
        *flags,
    ).first()
    if direct is not None:
        return True

    turn_tokens = ForgetVisibilityService.turn_tokens_by_event(
        db, [event_id]
    ).get(event_id, set())
    if turn_tokens:
        turn_hit = db.query(ForgetTombstone.id).filter(
            ForgetTombstone.target_type == "turn_record",
            or_(
                ForgetTombstone.target_id.in_(turn_tokens),
                ForgetTombstone.source_turn_record_id.in_(turn_tokens),
            ),
            *flags,
        ).first()
        if turn_hit is not None:
            return True

    mem_ids = {
        r[0] for r in db.query(ContentProvenanceRef.owner_id).filter(
            ContentProvenanceRef.owner_type == "memory_record",
            ContentProvenanceRef.source_type == "event",
            ContentProvenanceRef.source_id == event_id,
        ).all() if r[0]
    }
    if mem_ids:
        mem_hit = db.query(ForgetTombstone.id).filter(
            ForgetTombstone.target_type == "memory_record",
            ForgetTombstone.target_id.in_(mem_ids),
            *flags,
        ).first()
        if mem_hit is not None:
            return True
    return False


def _verify_source_event_integrity(
    db: Session, operation_id: str, op: ForgetOperation, memory_ids: list[str],
) -> list[str]:
    """校验 Memory 来源 Event 隔离与 Summary 重建的隐私闭环完整性。

    仅窄选择器调用（宽选择器最终为非严格的 verified_with_coarse_purge）。
    无来源 Event / 无受影响 Summary 时真空通过；一旦存在来源却无法证明其已被
    屏蔽、或 mode 内容语义不符、或新 Summary 仍引用被屏蔽 Event，即记为失败，
    从而禁止标记为严格 verified。
    """
    from sqlalchemy import func as sa_func

    from aiive.forget.visibility_service import ForgetVisibilityService

    failures: list[str] = []
    if not memory_ids:
        return failures

    # ── Memory → Event Provenance（持久，Evidence 删除后仍可反查）──
    src_ids = [
        r[0] for r in db.query(ContentProvenanceRef.source_id).filter(
            ContentProvenanceRef.owner_type == "memory_record",
            ContentProvenanceRef.source_type == "event",
            ContentProvenanceRef.owner_id.in_(memory_ids),
        ).distinct().all() if r[0]
    ]
    if src_ids:
        # (1) 来源 Event 屏蔽覆盖：统一可见性规则（含所有有效 Tombstone、
        #     TurnRecord 覆盖、Memory → Event Provenance）
        blocked = ForgetVisibilityService.blocked_target_ids(
            db, "event", [(eid, None) for eid in src_ids],
        )
        missed = [eid for eid in src_ids if eid not in blocked]
        if missed:
            failures.append(f"{len(missed)} memory source events not shielded")

        # (1b) 持久化保护：Verify 不能只依赖 active Shield。Shield 在成功后会
        #      retire，因此必须证明每个来源 Event 仍有 Tombstone 同时承担
        #      block_visibility（退休后不可见）与 block_reingestion（退休后不可重抽）。
        persistent_missed = [
            eid for eid in src_ids
            if not _has_persistent_source_event_protection(db, eid)
        ]
        if persistent_missed:
            failures.append(
                f"{len(persistent_missed)} source events lack persistent tombstone protection"
            )

        # (2) mode 内容语义：memory_only 保留原文；history_only/everywhere 清理
        ev_by_id = {
            e.id: e for e in db.query(Event).filter(Event.id.in_(src_ids)).all()
        }
        for eid in src_ids:
            ev = ev_by_id.get(eid)
            if ev is None:
                continue
            empty = not ev.payload
            if op.mode == "memory_only":
                if empty:
                    failures.append(
                        f"memory_only source event {eid} payload wrongly cleared"
                    )
            elif op.mode in ("history_only", "everywhere"):
                purged = db.query(ForgetTombstone.id).filter(
                    ForgetTombstone.target_type == "event",
                    ForgetTombstone.target_id == eid,
                    ForgetTombstone.content_purged.is_(True),
                ).first() is not None
                if not empty and not purged:
                    failures.append(
                        f"{op.mode} source event {eid} payload not purged"
                    )

    # ── 受影响的新 Summary 不再引用被屏蔽 Event ──
    affected_segments = [
        r[0] for r in (
            db.query(SegmentSummary.segment_id)
            .join(ForgetDependency,
                  ForgetDependency.dependency_id == SegmentSummary.id)
            .filter(
                ForgetDependency.forget_operation_id == operation_id,
                ForgetDependency.dependency_type == "segment_summary",
            )
            .distinct().all()
        ) if r[0]
    ]
    if affected_segments:
        max_versions = dict(
            db.query(
                SegmentSummary.segment_id,
                sa_func.max(SegmentSummary.summary_version),
            ).filter(
                SegmentSummary.segment_id.in_(affected_segments),
            ).group_by(SegmentSummary.segment_id).all()
        )
        current = db.query(SegmentSummary).filter(
            SegmentSummary.segment_id.in_(affected_segments),
        ).all()
        ref_ids: set[str] = set()
        for s in current:
            if max_versions.get(s.segment_id) == s.summary_version:
                ref_ids.update(str(x) for x in (s.source_event_ids or []) if x)
        if ref_ids:
            blocked_refs = ForgetVisibilityService.blocked_target_ids(
                db, "event", [(eid, None) for eid in ref_ids],
            )
            leaked = ref_ids & blocked_refs
            if leaked:
                failures.append(
                    f"{len(leaked)} blocked events still referenced by rebuilt summaries"
                )
    return failures


def handle_forget_verify(claimed: ClaimedJob) -> HandlerResult:
    """Phase N: 验证所有读取路径不返回 forgotten 内容。

    检查: memory forgotten / retrieval tombstone / tombstone 存在 / provenance
    输出: verified / verified_with_coarse_purge / legacy_unverifiable
    """
    payload = claimed.payload
    operation_id = payload.get("forget_operation_id", "")
    if not operation_id:
        return HandlerResult(HandlerOutcome.NON_RETRYABLE, "missing forget_operation_id")

    db = SessionLocal()
    try:
        op = db.query(ForgetOperation).filter_by(id=operation_id).first()
        if not op:
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "operation not found")

        failures: list[str] = []
        is_wide = (op.selector_type not in ("memory_ids", "turn_ids", "event_ids"))

        # 收集 memory targets（包括宽选择器通过 cascade 物化的）
        targets = (
            db.query(ForgetTarget)
            .filter_by(forget_operation_id=operation_id, target_type="memory_record")
            .all()
        )
        memory_ids = [t.target_id for t in targets]

        # 1. memory_records forgotten
        if memory_ids:
            visible = db.query(MemoryRecord.id).filter(
                MemoryRecord.id.in_(memory_ids),
                MemoryRecord.lifecycle_state != "forgotten",
            ).first()
            if visible:
                failures.append(f"memory_record {visible.id} not forgotten")

        # 2. retrieval 不再可搜索
        if memory_ids:
            leak = db.query(RetrievalIndexEntry.id).filter(
                RetrievalIndexEntry.source_type == "memory_record",
                RetrievalIndexEntry.source_id.in_(memory_ids),
                RetrievalIndexEntry.is_searchable.is_(True),
            ).first()
            if leak:
                failures.append(f"retrieval entry {leak.id} still searchable")

        # 3. Tombstone 覆盖（memory_record）
        if memory_ids:
            tomb_ids = {row[0] for row in db.query(ForgetTombstone.target_id).filter(
                ForgetTombstone.forget_operation_id == operation_id,
                ForgetTombstone.target_type == "memory_record",
            ).all()}
            missing = [mid for mid in memory_ids if mid not in tomb_ids]
            if missing:
                failures.append(f"{len(missing)} targets missing tombstone")

        # 3b. event / turn_record 目标按正确类型校验 Tombstone 覆盖（#2：
        # turn/event 选择器的目标不得以 memory_record 名义误报成功）
        for ttype in ("event", "turn_record"):
            typed_targets = (
                db.query(ForgetTarget.target_id)
                .filter_by(forget_operation_id=operation_id, target_type=ttype)
                .all()
            )
            typed_ids = [r[0] for r in typed_targets]
            if not typed_ids:
                continue
            typed_rows = db.query(
                ForgetTombstone.target_id,
                ForgetTombstone.block_visibility,
                ForgetTombstone.block_reingestion,
            ).filter(
                ForgetTombstone.forget_operation_id == operation_id,
                ForgetTombstone.target_type == ttype,
                ForgetTombstone.target_id.in_(typed_ids),
            ).all()
            typed_tombs = {row[0] for row in typed_rows}
            typed_missing = [tid for tid in typed_ids if tid not in typed_tombs]
            if typed_missing:
                failures.append(
                    f"{len(typed_missing)} {ttype} targets missing tombstone"
                )
            typed_bad_flags = [
                row[0] for row in typed_rows if not (row[1] and row[2])
            ]
            if typed_bad_flags:
                failures.append(
                    f"{len(typed_bad_flags)} {ttype} targets lack persistent visibility/reingestion tombstone"
                )

        # 3d. Turn 目标下的全部子 Event 必须获得长期 Event Tombstone（Cascade 后），
        #     否则 Shield 退休后子 Event 仍可能重新进入上下文或被重新抽取。
        turn_targets = db.query(ForgetTarget.target_id).filter_by(
            forget_operation_id=operation_id, target_type="turn_record",
        ).all()
        turn_target_ids = [r[0] for r in turn_targets]
        if turn_target_ids:
            turn_tokens = _normalize_turn_tokens(db, turn_target_ids)
            child_ids = [
                r[0] for r in db.query(Event.id).filter(
                    Event.turn_id.in_(turn_tokens),
                ).all()
            ]
            if child_ids:
                child_protected = {
                    r[0] for r in db.query(ForgetTombstone.target_id).filter(
                        ForgetTombstone.target_type == "event",
                        ForgetTombstone.target_id.in_(child_ids),
                        ForgetTombstone.block_visibility.is_(True),
                        ForgetTombstone.block_reingestion.is_(True),
                    ).all()
                }
                child_missing = [
                    eid for eid in child_ids if eid not in child_protected
                ]
                if child_missing:
                    failures.append(
                        f"{len(child_missing)} turn child events lack persistent tombstone"
                    )

        # 3c. Memory 来源 Event 隔离 / Summary 重建完整性（仅窄选择器需证明
        #     严格 verified；宽选择器最终为非严格 verified_with_coarse_purge）。
        #     无法证明完整性时，下面的失败会阻止落入严格 verified。
        if not is_wide:
            failures.extend(
                _verify_source_event_integrity(db, operation_id, op, memory_ids)
            )

        # 3e. 逐表内容完整性证明：重新读取数据库与对象存储，不信任 Scrub rowcount。
        #     每个纳入矩阵的字段都有残留计数；任何残留都进入 failed_retryable，
        #     Shield 保持 active。缺少 manifest 的历史操作无法证明范围，保持旧行为。
        from aiive.forget.content_scope import load_scope
        from aiive.forget.content_scrub import verify_scope
        content_report = None
        scope = load_scope(db, operation_id, op.mode)
        if scope is not None:
            content_report = verify_scope(db, scope)
            # 补入 Scrub 阶段的逐表执行计数，形成「目标/执行/残留/对象」完整证明。
            scrub_action = db.query(ForgetAction).filter_by(
                idempotency_key=f"fa:{operation_id}:scrub_content:global:b0",
            ).first()
            if scrub_action is not None and isinstance(scrub_action.details, dict):
                counts = scrub_action.details.get("counts")
                if isinstance(counts, dict):
                    content_report.scrubbed.update({
                        str(k): int(v) for k, v in counts.items()
                    })
            failures.extend(content_report.failures)

        # 4. 判定结果（三态，spec §N）
        if failures:
            op.status = "failed_retryable"
            op.error_message = "; ".join(failures[:3])
        elif is_wide:
            # 宽选择器：逐表 Scrub 与 Verify 均通过 → verified_with_coarse_purge
            # （状态名沿用兼容；语义已由 verify_scope 的逐表证明支撑）。
            op.status = "verified_with_coarse_purge"
            op.verified_at = _utcnow()
            op.purged_at = _utcnow()
        elif op.legacy_request_id:
            # 旧 forget_requests 迁移而来，无完整 provenance 追踪 → 无法严格验证
            # #23：同样落 purged_at，否则 retention（按 purged_at 过滤）永不清理该操作
            op.status = "legacy_unverifiable"
            op.verified_at = _utcnow()
            op.purged_at = _utcnow()
        else:
            # 窄选择器：saga 内具备完整 tombstone + dependency 追踪 → verified
            op.status = "verified"
            op.verified_at = _utcnow()
            op.purged_at = _utcnow()

        # 5. 成功终态 → 该操作的 Shield 置 retired（#3：Shield 是临时即时屏蔽层，
        # 永久拦截由 Tombstone 承担；不 retire 会把该 thread/全系统的未来新数据
        # 永久屏蔽）。失败态保持 active（fail-closed）。
        if op.status in ("verified", "verified_with_coarse_purge", "legacy_unverifiable"):
            from aiive.db.forget_models import ForgetShield
            db.query(ForgetShield).filter(
                ForgetShield.forget_operation_id == operation_id,
                ForgetShield.status == "active",
            ).update({ForgetShield.status: "retired"}, synchronize_session=False)

        op.updated_at = _utcnow()
        verify_details: dict[str, Any] = {
            "result": op.status,
            "failures": len(failures),
            "failure_messages": failures[:20],
        }
        if content_report is not None:
            verify_details["content"] = content_report.to_dict()
        _record_verify_action(db, operation_id, verify_details)
        db.commit()

        result_msg = "verified" if not failures else f"failed: {failures[0]}"
        return HandlerResult(HandlerOutcome.COMPLETED, result_msg)
    except Exception as exc:
        db.rollback()
        logger.exception("forget_verify failed")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"verify: {exc}")
    finally:
        db.close()


def handle_forget_reconcile(claimed: ClaimedJob) -> HandlerResult:
    """对账单个 Forget Saga，并补发或自动恢复阻塞阶段。"""
    from aiive.forget.reconcile_service import ForgetReconcileService

    operation_id = claimed.payload.get("forget_operation_id", "")
    if not operation_id:
        return HandlerResult(HandlerOutcome.NON_RETRYABLE, "缺少 forget_operation_id")

    db = SessionLocal()
    try:
        result = ForgetReconcileService.reconcile_operation(db, operation_id)
        if result.action == "operation_not_found":
            db.rollback()
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "遗忘操作不存在")
        if result.action in {"duplicate_stage_run", "outbox_job_missing", "unsupported_job_status"}:
            db.rollback()
            return HandlerResult(
                HandlerOutcome.NON_RETRYABLE,
                f"遗忘阶段状态无法自动恢复: {result.action}:{result.stage or ''}",
            )
        db.commit()
        return HandlerResult(
            HandlerOutcome.COMPLETED,
            f"遗忘对账完成: {result.action}:{result.stage or ''}",
        )
    except Exception as exc:
        db.rollback()
        logger.exception("forget_reconcile 执行失败")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"遗忘对账失败: {exc}")
    finally:
        db.close()


# ── helpers ──


def _record_action(
    db: Session,
    operation_id: str,
    action_type: str,
    *,
    target_id: str | None = None,
    dependency_id: str | None = None,
    batch_no: int = 0,
    details: dict[str, Any] | None = None,
) -> None:
    """幂等写入 ForgetAction（spec P/§11）。

    使用 (operation_id, action_type, target_or_dep, batch_no) 构造
    idempotency_key 保证崩溃后重试不重复。
    """
    scope = target_id or dependency_id or "global"
    ikey = f"fa:{operation_id}:{action_type}:{scope}:b{batch_no}"
    existing = db.query(ForgetAction).filter_by(idempotency_key=ikey).first()
    if existing is not None:
        return
    now = _utcnow()
    action = ForgetAction(
        id=_generate_id(),
        forget_operation_id=operation_id,
        action_type=action_type,
        target_id=target_id,
        dependency_id=dependency_id,
        batch_no=batch_no,
        status="done",
        idempotency_key=ikey,
        details=details,
        started_at=now,
        completed_at=now,
    )
    db.add(action)


def _record_scrub_content_action(
    db: Session, operation_id: str, counts: dict[str, int],
) -> None:
    """持久化逐表执行计数（Purge 每次重试刷新）。"""
    ikey = f"fa:{operation_id}:scrub_content:global:b0"
    now = _utcnow()
    action = db.query(ForgetAction).filter_by(idempotency_key=ikey).first()
    if action is None:
        db.add(ForgetAction(
            id=_generate_id(),
            forget_operation_id=operation_id,
            action_type="scrub_content",
            batch_no=0,
            status="done",
            idempotency_key=ikey,
            details={"counts": counts},
            started_at=now,
            completed_at=now,
        ))
        return
    action.details = {"counts": counts}
    action.status = "done"
    action.completed_at = now


def _record_verify_action(
    db: Session, operation_id: str, details: dict[str, Any],
) -> None:
    """Verify 动作每次都刷新 details/状态（_record_action 是幂等插入，不适合）。"""
    ikey = f"fa:{operation_id}:verify:global:b0"
    now = _utcnow()
    action = db.query(ForgetAction).filter_by(idempotency_key=ikey).first()
    if action is None:
        db.add(ForgetAction(
            id=_generate_id(),
            forget_operation_id=operation_id,
            action_type="verify",
            batch_no=0,
            status="done",
            idempotency_key=ikey,
            details=details,
            started_at=now,
            completed_at=now,
        ))
        return
    action.details = details
    action.status = "done"
    action.completed_at = now


def _generate_id() -> str:
    import uuid
    return str(uuid.uuid4())
