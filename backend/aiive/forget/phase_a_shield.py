"""Phase A 立即屏蔽事务。

职责:
  单次短事务内完成:
    1. 创建 ForgetOperation
    2. 冻结 ForgetSelectorManifest
    3. 物化 ForgetShield（选择器级 + 实体级）
    4. 写入 ForgetTombstone
    5. 目标 MemoryRecord → forgotten
    6. CoreMemoryBlock 删除
    7. RetrievalIndexEntry tombstone
    8. enqueue Cascade OutboxJob + ForgetStageRun

  只有 Phase A 成功，工具才返回 "forget accepted and shielded"。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from aiive.db.forget_models import (
    ForgetOperation,
    ForgetSelectorManifest,
    ForgetShield,
    ForgetStageRun,
    ForgetTarget,
    ForgetTombstone,
)
from aiive.db.models import (
    CoreMemoryBlock,
    MemoryRecord,
    MemoryVectorProjection,
    OutboxJob,
    RetrievalIndexEntry,
    RetrievalIndexToken,
)
from aiive.forget.fingerprint import compute_value_fingerprint
from aiive.forget.selector_normalizer import (
    MAX_INLINE_SHIELD_TARGETS,
    compute_normalized_shield_key,
    compute_selector_hash,
    is_inline_shield_candidate,
    normalize_selector,
)

logger = logging.getLogger(__name__)

# 受保护的 external_world writing 工具（非删除性，仅屏蔽）
_SHIELD_FORGET_OPERATION_KEY_PREFIX = "forget:"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


# ═══════════════════════════════════════════════════════════════════
# Phase A 主入口
# ═══════════════════════════════════════════════════════════════════


def execute_phase_a_shield(
    session: Session,
    *,
    mode: str,
    memory_ids: list[str] | None = None,
    turn_ids: list[str] | None = None,
    event_ids: list[str] | None = None,
    thread_id: str | None = None,
    canonical_key: str | None = None,
    scope_type: str | None = None,
    scope_id: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    all_user_data: bool = False,
    reason: str = "",
    requested_by: str = "",
    operation_key: str | None = None,
) -> dict[str, Any]:
    """执行 Phase A 立即屏蔽事务。

    必须在调用方的 session 事务边界内调用；不单独管理事务。

    Returns:
        { "operation_id": str, "operation_key": str, "status": "shielded",
          "shielded_at": str, "target_count": int }
    """

    now = _utcnow()
    op_id = _new_id()
    op_key = operation_key or f"{_SHIELD_FORGET_OPERATION_KEY_PREFIX}{op_id}"

    # 1. 规范化 selector
    selector_payload = normalize_selector(
        mode=mode,
        memory_ids=memory_ids,
        turn_ids=turn_ids,
        event_ids=event_ids,
        thread_id=thread_id,
        canonical_key=canonical_key,
        scope_type=scope_type,
        scope_id=scope_id,
        time_from=time_from,
        time_to=time_to,
        all_user_data=all_user_data,
    )
    selector_hash = compute_selector_hash(selector_payload)
    cutoff = now  # 只处理早于当前时刻的数据

    # 2. 创建 ForgetOperation
    operation = ForgetOperation(
        id=op_id,
        operation_key=op_key,
        mode=mode,
        selector_type=selector_payload["selector_type"],
        selector_hash=selector_hash,
        status="shielded",
        requested_by=requested_by,
        reason_code=reason[:64] if reason else None,
        target_count=0,
        shielded_at=now,
    )
    session.add(operation)

    # 3. 冻结 ForgetSelectorManifest
    manifest = ForgetSelectorManifest(
        id=_new_id(),
        forget_operation_id=op_id,
        selector_type=selector_payload["selector_type"],
        selector_payload=selector_payload,
        selector_payload_hash=selector_hash,
        cutoff_created_at=cutoff,
        frozen_at=now,
    )
    session.add(manifest)

    # 4. 物化选择器级 Shield
    _materialize_selector_shields(
        session, op_id, selector_payload, cutoff, now,
    )

    # 5. 实体级 Shield（仅 bounded explicit IDs）
    _materialize_entity_shields(
        session, op_id, selector_payload, now,
    )

    # 6. Tombstone（仅 bounded explicit IDs / memory）
    target_count = _write_tombstones_and_forget_memories(
        session, op_id, mode, memory_ids, turn_ids, event_ids, now,
    )

    # 7. 仅 bounded explicit Memory IDs 冻结 Target
    if memory_ids and len(memory_ids) <= MAX_INLINE_SHIELD_TARGETS:
        _freeze_initial_targets(session, op_id, memory_ids, now)

    # 8. CoreMemoryBlock 删除（直接删相关 block）
    _delete_core_memory_blocks(session, memory_ids)

    # 9. RetrievalIndexEntry tombstone（仅 bounded memory_ids）
    if memory_ids:
        _tombstone_retrieval_entries(session, memory_ids, now)

    # 10. 遗忘 Shield 生效后重建文件投影，确保宽选择器也立即从派生视图隐藏。
    from aiive.config import settings
    if settings.aiive_memory_file_projection_enabled:
        session.add(OutboxJob(
            operation_id=f"forget:{op_id}:file_projection",
            job_type="memory_markdown_project",
            status="pending",
            payload={"schema_version": 1, "forget_operation_id": op_id},
            trace_id=op_id,
            max_retries=3,
        ))

    # 11. enqueue Cascade OutboxJob + ForgetStageRun
    cascade_op_id = f"forget:{op_id}:cascade"
    cascade_job = OutboxJob(
        operation_id=cascade_op_id,
        job_type="forget_cascade",
        status="pending",
        payload={
            "forget_operation_id": op_id,
            "operation_key": op_key,
            "mode": mode,
        },
        max_retries=3,
    )
    session.add(cascade_job)
    session.flush()  # 获取 cascade_job.id

    stage_run = ForgetStageRun(
        id=_new_id(),
        forget_operation_id=op_id,
        stage="cascade",
        outbox_job_id=cascade_job.id,
        status="pending",
    )
    session.add(stage_run)

    # 11. 更新 operation target_count
    operation.target_count = target_count

    return {
        "operation_id": op_id,
        "operation_key": op_key,
        "status": "shielded",
        "shielded_at": now.isoformat(),
        "target_count": target_count,
    }


# ═══════════════════════════════════════════════════════════════════
# 子步骤
# ═══════════════════════════════════════════════════════════════════


def _materialize_selector_shields(
    session: Session,
    operation_id: str,
    selector_payload: dict[str, Any],
    cutoff: datetime,
    now: datetime,
) -> None:
    """物化选择器级 Shield（不枚举 Target）。"""
    selector_type = selector_payload["selector_type"]

    if selector_type in ("all_user_data",):
        shield = ForgetShield(
            id=_new_id(),
            forget_operation_id=operation_id,
            selector_type=selector_type,
            all_user_data=True,
            cutoff_created_at=cutoff,
            status="active",
            normalized_shield_key=compute_normalized_shield_key(
                operation_id, selector_type,
                cutoff_created_at=cutoff,
            ),
            created_at=now,
        )
        session.add(shield)
        return

    if selector_type == "thread" and selector_payload.get("thread_id"):
        shield = ForgetShield(
            id=_new_id(),
            forget_operation_id=operation_id,
            selector_type=selector_type,
            thread_id=selector_payload["thread_id"],
            cutoff_created_at=cutoff,
            status="active",
            normalized_shield_key=compute_normalized_shield_key(
                operation_id, selector_type,
                thread_id=selector_payload["thread_id"],
                cutoff_created_at=cutoff,
            ),
            created_at=now,
        )
        session.add(shield)
        return

    if selector_type == "time_range":
        shield = ForgetShield(
            id=_new_id(),
            forget_operation_id=operation_id,
            selector_type=selector_type,
            time_from=selector_payload.get("time_from"),
            time_to=selector_payload.get("time_to"),
            cutoff_created_at=cutoff,
            status="active",
            normalized_shield_key=compute_normalized_shield_key(
                operation_id, selector_type,
                cutoff_created_at=cutoff,
            ),
            created_at=now,
        )
        session.add(shield)
        return

    if selector_type == "scope" and selector_payload.get("scope_type"):
        shield = ForgetShield(
            id=_new_id(),
            forget_operation_id=operation_id,
            selector_type=selector_type,
            scope_type=selector_payload["scope_type"],
            scope_id=selector_payload.get("scope_id"),
            cutoff_created_at=cutoff,
            status="active",
            normalized_shield_key=compute_normalized_shield_key(
                operation_id, selector_type,
                scope_type=selector_payload["scope_type"],
                scope_id=selector_payload.get("scope_id"),
                cutoff_created_at=cutoff,
            ),
            created_at=now,
        )
        session.add(shield)
        return

    if selector_type == "canonical_key" and selector_payload.get("canonical_key"):
        shield = ForgetShield(
            id=_new_id(),
            forget_operation_id=operation_id,
            selector_type=selector_type,
            canonical_key=selector_payload["canonical_key"],
            cutoff_created_at=cutoff,
            status="active",
            normalized_shield_key=compute_normalized_shield_key(
                operation_id, selector_type,
                cutoff_created_at=cutoff,
            ),
            created_at=now,
        )
        session.add(shield)
        return

    # memory_ids / turn_ids / event_ids: only selector-level if exceeds inline limit
    if not is_inline_shield_candidate(selector_payload):
        shield = ForgetShield(
            id=_new_id(),
            forget_operation_id=operation_id,
            selector_type=selector_type,
            cutoff_created_at=cutoff,
            status="active",
            normalized_shield_key=compute_normalized_shield_key(
                operation_id, selector_type,
                cutoff_created_at=cutoff,
            ),
            created_at=now,
        )
        session.add(shield)


def _materialize_entity_shields(
    session: Session,
    operation_id: str,
    selector_payload: dict[str, Any],
    now: datetime,
) -> None:
    """微信 bounded explicit IDs 写实体级 Shield。

    仅 memory_ids / turn_ids / event_ids（≤MAX_INLINE_SHIELD_TARGETS）。
    canonical_key / thread / time_range / scope / all_user_data / 超限 ID 不写实体级。
    """
    if not is_inline_shield_candidate(selector_payload):
        return

    selector_type = selector_payload["selector_type"]
    if selector_type == "memory_ids":
        for mid in selector_payload.get("memory_ids", []):
            shield = ForgetShield(
                id=_new_id(),
                forget_operation_id=operation_id,
                selector_type=selector_type,
                target_type="memory_record",
                target_id=mid,
                cutoff_created_at=now,
                status="active",
                normalized_shield_key=compute_normalized_shield_key(
                    operation_id, selector_type,
                    target_type="memory_record", target_id=mid,
                ),
                created_at=now,
            )
            session.add(shield)
    elif selector_type == "turn_ids":
        for tid in selector_payload.get("turn_ids", []):
            shield = ForgetShield(
                id=_new_id(),
                forget_operation_id=operation_id,
                selector_type=selector_type,
                target_type="turn_record",
                target_id=tid,
                cutoff_created_at=now,
                status="active",
                normalized_shield_key=compute_normalized_shield_key(
                    operation_id, selector_type,
                    target_type="turn_record", target_id=tid,
                ),
                created_at=now,
            )
            session.add(shield)
    elif selector_type == "event_ids":
        for eid in selector_payload.get("event_ids", []):
            shield = ForgetShield(
                id=_new_id(),
                forget_operation_id=operation_id,
                selector_type=selector_type,
                target_type="event",
                target_id=eid,
                cutoff_created_at=now,
                status="active",
                normalized_shield_key=compute_normalized_shield_key(
                    operation_id, selector_type,
                    target_type="event", target_id=eid,
                ),
                created_at=now,
            )
            session.add(shield)


def _write_tombstones_and_forget_memories(
    session: Session,
    operation_id: str,
    mode: str,
    memory_ids: list[str] | None,
    turn_ids: list[str] | None,
    event_ids: list[str] | None,
    now: datetime,
) -> int:
    """写入 Tombstone 并将目标 MemoryRecord 置为 forgotten。

    仅对 bounded explicit IDs 处理；宽选择器的 Tombstone 由 Cascade Batch 补充。
    """
    target_count = 0

    is_audit_readable = (mode == "memory_only")

    # Memory Tombstones
    if memory_ids and len(memory_ids) <= MAX_INLINE_SHIELD_TARGETS:
        for mid in memory_ids:
            # 计算内容指纹（HMAC-SHA256）：用于 reingestion 时按 canonical_key+value_fingerprint 拦截
            value_fingerprint = None
            fingerprint_key_version = None
            rec = session.query(MemoryRecord).filter(MemoryRecord.id == mid).first()
            if rec is not None:
                try:
                    value_fingerprint, fingerprint_key_version = compute_value_fingerprint(
                        f"{rec.canonical_key}|{rec.content or ''}"
                    )
                except Exception:
                    logger.warning("计算 memory %s 内容指纹失败，跳过指纹写入", mid)
            tombstone = ForgetTombstone(
                id=_new_id(),
                forget_operation_id=operation_id,
                target_type="memory_record",
                target_id=mid,
                canonical_key=rec.canonical_key if rec else None,
                value_fingerprint=value_fingerprint,
                fingerprint_key_version=fingerprint_key_version,
                block_visibility=True,
                block_reingestion=True,
                content_purged=False,
                allow_audit_read=is_audit_readable,
                reason_code="forget",
                created_at=now,
            )
            session.add(tombstone)
            target_count += 1

        # 置 MemoryRecord.lifecycle_state = forgotten
        session.query(MemoryRecord).filter(
            MemoryRecord.id.in_(memory_ids)
        ).update(
            {"lifecycle_state": "forgotten", "updated_at": now},
            synchronize_session="fetch",
        )
        session.query(MemoryVectorProjection).filter(
            MemoryVectorProjection.memory_id.in_(memory_ids)
        ).delete(synchronize_session=False)

    # Event Tombstones (memory_only: allow_audit_read=true)
    if event_ids and len(event_ids) <= MAX_INLINE_SHIELD_TARGETS:
        for eid in event_ids:
            tombstone = ForgetTombstone(
                id=_new_id(),
                forget_operation_id=operation_id,
                target_type="event",
                target_id=eid,
                block_visibility=True,
                block_reingestion=True,
                content_purged=False,
                allow_audit_read=is_audit_readable,
                reason_code="forget",
                source_event_id=eid,
                created_at=now,
            )
            session.add(tombstone)
            target_count += 1

    # Turn Tombstones
    if turn_ids and len(turn_ids) <= MAX_INLINE_SHIELD_TARGETS:
        for tid in turn_ids:
            tombstone = ForgetTombstone(
                id=_new_id(),
                forget_operation_id=operation_id,
                target_type="turn_record",
                target_id=tid,
                block_visibility=True,
                block_reingestion=True,
                content_purged=False,
                allow_audit_read=is_audit_readable,
                reason_code="forget",
                source_turn_record_id=tid,
                created_at=now,
            )
            session.add(tombstone)
            target_count += 1

    return target_count


def _freeze_initial_targets(
    session: Session,
    operation_id: str,
    memory_ids: list[str],
    now: datetime,
) -> None:
    """仅对 bounded explicit Memory IDs 冻结初始 Target（Phase B 不再重新发现）。"""
    for mid in memory_ids:
        target = ForgetTarget(
            id=_new_id(),
            forget_operation_id=operation_id,
            target_type="memory_record",
            target_id=mid,
            batch_no=0,
            frozen_at=now,
        )
        session.add(target)


def _delete_core_memory_blocks(
    session: Session,
    memory_ids: list[str] | None,
) -> None:
    """删除/剥离关联的 CoreMemoryBlock，防止被忘内容经 Core Memory 泄漏。

    仅 bounded explicit memory_ids 在 Phase A 精确处理；宽选择器（memory_ids 为
    None）由 Cascade 阶段细粒度处理。被忘 memory 从 block.source_memory_ids 移除；
    若 block 不再引用任何 memory，则整块删除。
    """
    if not memory_ids:
        return
    for mid in memory_ids:
        # source_memory_ids 是 JSON 列；PostgreSQL 的 json 类型不支持 @> 包含
        # 运算，需先 cast 成 jsonb。直接用 .contains() 会退化成 LIKE 报
        # 「operator does not exist: json ~~ text」。
        blocks = (
            session.query(CoreMemoryBlock)
            .filter(cast(CoreMemoryBlock.source_memory_ids, JSONB).contains([mid]))
            .all()
        )
        for block in blocks:
            remaining = [m for m in (block.source_memory_ids or []) if m != mid]
            if not remaining:
                session.delete(block)
            else:
                block.source_memory_ids = remaining
                block.projection_version = (block.projection_version or 1) + 1
                block.updated_at = _utcnow()

def _tombstone_retrieval_entries(
    session: Session,
    memory_ids: list[str],
    _now: datetime,
) -> None:
    """将关联的 RetrievalIndexEntry 清正文 + 删 token。

    仅 bounded memory_ids 时执行；宽选择器由 Cascade Batch 处理。
    """
    if not memory_ids:
        return
    entries = (
        session.query(RetrievalIndexEntry)
        .filter(
            RetrievalIndexEntry.source_type == "memory_record",
            RetrievalIndexEntry.source_id.in_(memory_ids),
            RetrievalIndexEntry.is_searchable.is_(True),
        )
        .all()
    )
    for entry in entries:
        entry.search_text = ""
        entry.snippet = ""
        entry.title = "[forgotten]"
        entry.is_searchable = False
        # 删关联 token
        session.query(RetrievalIndexToken).filter(
            RetrievalIndexToken.entry_id == entry.id
        ).delete()
