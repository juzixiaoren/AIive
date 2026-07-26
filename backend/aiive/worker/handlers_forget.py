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


# selector_type → ForgetTarget.target_type 映射（turn/event 选择器不得写成 memory_record）
_SELECTOR_TARGET_TYPE: dict[str, str] = {
    "turn_ids": "turn_record",
    "event_ids": "event",
}


def _selector_target_type(selector_type: str) -> str:
    return _SELECTOR_TARGET_TYPE.get(selector_type, "memory_record")


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
    - 由 event/turn 反查 MemoryEvidence.source_event_id 派生 memory_record 目标
      一并处理（由被忘 turn/event 抽取出的派生记忆也要遗忘）。

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
        turn_event_rows = db.query(Event.id).filter(
            Event.turn_id.in_(turn_target_ids),
        ).all()
        derived_event_ids.extend(r[0] for r in turn_event_rows)
    if derived_event_ids:
        derived_memory_ids = {
            r[0] for r in db.query(MemoryEvidence.memory_id).filter(
                MemoryEvidence.source_event_id.in_(derived_event_ids),
            ).distinct().all()
        }
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
    for eid in event_target_ids:
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
    elif target_type == "event":
        src_event_ids = [target_id]
    elif target_type == "turn_record":
        src_event_ids = [
            r[0] for r in db.query(Event.id).filter(
                Event.turn_id == target_id,
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
            token_count=int(summary_payload.get("token_count") or 0),
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

    # 获取 segment 的 Event（过滤已被 tombstone block 的 event）
    from aiive.db.forget_models import ForgetTombstone
    blocked_ids = {
        row[0] for row in db.query(ForgetTombstone.target_id).filter(
            ForgetTombstone.forget_operation_id == operation_id,
            ForgetTombstone.target_type == "event",
            ForgetTombstone.block_visibility.is_(True),
        ).all()
    }

    # 经 TurnRecord.segment_id 关联取「该 segment」的事件（Event 无 segment_id），
    # 按 (turn_sequence, turn_event_index) 确定性排序（参考 raw_history_expander）。
    events = (
        db.query(Event, TurnRecord.turn_id)
        .join(TurnRecord, TurnRecord.turn_id == Event.turn_id)
        .filter(TurnRecord.segment_id == segment_id)
        .order_by(TurnRecord.turn_sequence.asc(), Event.turn_event_index.asc())
        .limit(50)
        .all()
    )
    remaining: list[tuple[Event, str]] = [
        (e, tid) for e, tid in events if e.id not in blocked_ids
    ]
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
        resp = llm.chat(messages, model=settings.aiive_llm_model, temperature=0)
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
            _scrub_batch(db, target_ids)
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

        # 宽选择器全量 scrub
        _scrub_wide(db, operation_id)

        # 全部完成 → enqueue verify（必须在 commit 之前，同事务）
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
    if turn_ids:
        # turn 选择器的 ID 语义为 TurnRecord.turn_id（Event.turn_id 同名关联），
        # 同时兼容直接传 turn_records.id 的旧数据。
        db.execute(sa_text(
            "UPDATE turn_records SET response_payload = NULL "
            "WHERE turn_id IN :ids OR id IN :ids"
        ).bindparams(ids_bind), {"ids": turn_ids})
        # 该 turn 下的事件正文一并清除
        db.execute(sa_text(
            "UPDATE events SET payload = '{}' WHERE turn_id IN :ids"
        ).bindparams(ids_bind), {"ids": turn_ids})

    for t in tombs:
        t.content_purged = True
        t.purged_at = now
    db.flush()


def _scrub_batch(db: Session, memory_ids: list[str]) -> None:
    """批量 scrub 内容——覆盖规格 §M 第 0 节清单所有内容承载位置。

    ids 使用 SQLAlchemy expanding 绑定参数，SQLite/PostgreSQL 均兼容
    （SQLite 渲染 IN (:1,:2,...)，PostgreSQL 渲染 = ANY(:ids)）。
    """
    from sqlalchemy import bindparam, text as sa_text
    now = _utcnow()
    ids_bind = bindparam("ids", expanding=True)

    # --- 前置：计算专属来源 event（必须在 DELETE evidence 之前）---
    # 共享来源 event 不在窄选择器内 scrub，避免破坏其他仍有效的 memory。
    event_ids = [
        row[0] for row in db.query(MemoryEvidence.source_event_id).filter(
            MemoryEvidence.memory_id.in_(memory_ids),
            MemoryEvidence.source_event_id.isnot(None),
        ).distinct().all()
    ]
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
        + "updated_at = :now WHERE id IN :ids"
    ).bindparams(ids_bind), {"now": now, "ids": memory_ids})
    db.execute(sa_text(
        "UPDATE memory_proposals SET raw_payload = NULL, normalized_payload = NULL "
        + "WHERE final_memory_id IN :ids"
    ).bindparams(ids_bind), {"ids": memory_ids})
    db.execute(sa_text(
        "DELETE FROM memory_evidence WHERE memory_id IN :ids"
    ).bindparams(ids_bind), {"ids": memory_ids})
    db.execute(sa_text(
        "UPDATE memory_maintenance_inputs SET snapshot_json = NULL "
        + "WHERE memory_record_id IN :ids"
    ).bindparams(ids_bind), {"ids": memory_ids})

    # --- retrieval index ---
    db.execute(sa_text(
        "UPDATE retrieval_index_entries SET search_text = '', snippet = '', "
        + "title = '[forgotten]', is_searchable = false "
        + "WHERE source_type = 'memory_record' AND source_id IN :ids"
    ).bindparams(ids_bind), {"ids": memory_ids})

    # --- 来源 Event scrub ---
    if exclusive_event_ids:
        db.execute(sa_text(
            "UPDATE events SET payload = '{}' WHERE id IN :ids"
        ).bindparams(ids_bind), {"ids": exclusive_event_ids})


def _scrub_wide(db: Session, operation_id: str) -> None:
    """宽选择器全量 scrub：覆盖规格 §M 清单所有内容承载位置。

    按选择器范围（thread / time_range / all_user_data）对 events / turn_records /
    working_states / context_snapshots / llm_calls / tasks / artifacts /
    compaction_inputs / epoch_compaction_inputs / segment_summaries /
    epoch_checkpoints 做 redact。
    """
    from sqlalchemy import text as sa_text

    op = db.query(ForgetOperation).filter_by(id=operation_id).first()
    if not op:
        return

    selector = (
        db.query(ForgetSelectorManifest)
        .filter_by(forget_operation_id=operation_id).first()
    )
    if not selector:
        return

    payload = selector.selector_payload
    thread_id = payload.get("thread_id")
    tf = payload.get("time_from")
    tt = payload.get("time_to")
    all_user = payload.get("all_user_data", False)

    # thread 范围
    if thread_id:
        db.execute(sa_text(
            "UPDATE events SET payload = '{}' WHERE thread_id = :tid"
        ), {"tid": thread_id})
        db.execute(sa_text(
            "UPDATE turn_records SET response_payload = NULL WHERE thread_id = :tid"
        ), {"tid": thread_id})
        db.execute(sa_text(
            "UPDATE working_states SET current_objective=NULL, open_loops='[]', "
            + "active_constraints='[]', pending_approvals='[]', artifact_refs='[]', "
            + "verified_tool_states='[]', uncommitted_side_effects='[]', "
            + "running_tool_state='[]' WHERE thread_id = :tid"
        ), {"tid": thread_id})
        db.execute(sa_text(
            "UPDATE context_snapshots SET context_items='[]' WHERE thread_id = :tid"
        ), {"tid": thread_id})
        db.execute(sa_text(
            "UPDATE llm_calls SET input_preview='', output_preview='' WHERE thread_id = :tid"
        ), {"tid": thread_id})
        db.execute(sa_text(
            "UPDATE tasks SET description='', title='[redacted]' WHERE thread_id = :tid"
        ), {"tid": thread_id})
        db.execute(sa_text(
            "UPDATE artifacts SET content='[redacted]' WHERE thread_id = :tid"
        ), {"tid": thread_id})
        db.execute(sa_text(
            "UPDATE compaction_inputs SET working_state_snapshot='{}' "
            + "WHERE segment_id IN (SELECT id FROM segments WHERE thread_id=:tid)"
        ), {"tid": thread_id})
        db.execute(sa_text(
            "UPDATE epoch_compaction_inputs SET current_objective=NULL, open_loops='[]', "
            + "active_constraints='[]', artifact_refs='[]', verified_tool_states='[]', "
            + "source_segment_ids='[]', source_hashes='[]' "
            + "WHERE epoch_id IN (SELECT id FROM epochs WHERE thread_id=:tid)"
        ), {"tid": thread_id})
        db.execute(sa_text(
            "UPDATE segment_summaries SET goal='[redacted]',outcome='[redacted]',"
            + "decisions='[]',open_loops='[]',entities='[]' WHERE source_event_ids "
            + "IN (SELECT id FROM events WHERE thread_id=:tid)"
        ), {"tid": thread_id})
        db.execute(sa_text(
            "UPDATE epoch_checkpoints SET current_goal='[redacted]',open_loops='[]',"
            + "active_constraints='[]' WHERE source_segment_ids IN "
            + "(SELECT id FROM segments WHERE thread_id=:tid)"
        ), {"tid": thread_id})

    # time range
    if tf or tt:
        conditions = []
        params: dict[str, Any] = {}
        if tf:
            conditions.append("created_at >= :tf")
            params["tf"] = tf
        if tt:
            conditions.append("created_at <= :tt")
            params["tt"] = tt
        where = " AND ".join(conditions)
        db.execute(sa_text(
            f"UPDATE events SET payload = '{{}}' WHERE {where}"
        ), params)
        db.execute(sa_text(
            f"UPDATE llm_calls SET input_preview='', output_preview='' WHERE {where}"
        ), params)
        db.execute(sa_text(
            f"UPDATE context_snapshots SET context_items='[]' WHERE {where}"
        ), params)
        db.execute(sa_text(
            f"UPDATE tasks SET description='', title='[redacted]' WHERE {where}"
        ), params)
        db.execute(sa_text(
            f"UPDATE compaction_inputs SET working_state_snapshot='{{}}' WHERE {where}"
        ), params)
        db.execute(sa_text(
            f"UPDATE epoch_compaction_inputs SET current_objective=NULL, open_loops='[]', "
            + f"active_constraints='[]', artifact_refs='[]', verified_tool_states='[]', "
            + f"source_segment_ids='[]', source_hashes='[]' WHERE {where}"
        ), params)

    # all_user_data
    if all_user:
        db.execute(sa_text("UPDATE events SET payload = '{}'"))
        db.execute(sa_text("UPDATE turn_records SET response_payload = NULL"))
        db.execute(sa_text(
            "UPDATE working_states SET current_objective=NULL, open_loops='[]', "
            + "active_constraints='[]', pending_approvals='[]', artifact_refs='[]', "
            + "verified_tool_states='[]', uncommitted_side_effects='[]', "
            + "running_tool_state='[]'"
        ))
        db.execute(sa_text("UPDATE context_snapshots SET context_items='[]'"))
        db.execute(sa_text("UPDATE llm_calls SET input_preview='', output_preview=''"))
        db.execute(sa_text("UPDATE tasks SET description='', title='[redacted]'"))
        db.execute(sa_text("UPDATE artifacts SET content='[redacted]'"))
        db.execute(sa_text("UPDATE compaction_inputs SET working_state_snapshot='{}'"))
        db.execute(sa_text(
            "UPDATE epoch_compaction_inputs SET current_objective=NULL, open_loops='[]', "
            + "active_constraints='[]', artifact_refs='[]', verified_tool_states='[]', "
            + "source_segment_ids='[]', source_hashes='[]'"
        ))
        db.execute(sa_text(
            "UPDATE segment_summaries SET goal='[redacted]',outcome='[redacted]',"
            + "decisions='[]',open_loops='[]',entities='[]'"
        ))
        db.execute(sa_text(
            "UPDATE epoch_checkpoints SET current_goal='[redacted]',open_loops='[]',"
            + "active_constraints='[]'"
        ))
    db.flush()


# ═══════════════════════════════════════════════════════════════════
# forget_verify — Phase N: ForgetVerifier
# ═══════════════════════════════════════════════════════════════════


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
            typed_tombs = {row[0] for row in db.query(ForgetTombstone.target_id).filter(
                ForgetTombstone.forget_operation_id == operation_id,
                ForgetTombstone.target_type == ttype,
            ).all()}
            typed_missing = [tid for tid in typed_ids if tid not in typed_tombs]
            if typed_missing:
                failures.append(
                    f"{len(typed_missing)} {ttype} targets missing tombstone"
                )

        # 4. 判定结果（三态，spec §N）
        if failures:
            op.status = "failed_retryable"
            op.error_message = "; ".join(failures[:3])
        elif is_wide:
            # 宽选择器：粗粒度 purge 已施加（_scrub_wide）→ verified_with_coarse_purge
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
        _record_action(db, operation_id, "verify",
                       details={"result": op.status, "failures": len(failures)})
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


def _generate_id() -> str:
    import uuid
    return str(uuid.uuid4())
