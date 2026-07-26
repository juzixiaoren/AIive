"""
Phase 6B 删除前安全谓词（Q.5）。

每种清理对象在扫描时与真正删除前都必须回源校验本模块谓词。
任一项不满足则跳过该对象，禁止删除。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session


def is_not_current_summary_or_checkpoint(
    session: Session,
    segment_summary_id: str | None = None,
    epoch_checkpoint_id: str | None = None,
) -> bool:
    """不是当前 Segment.summary_id / Epoch.checkpoint_id 指针。

    返回 True 表示安全可删除（不是当前指针）。
    """
    from aiive.db.models import Epoch, Segment

    if segment_summary_id:
        count = (
            session.query(Segment)
            .filter(Segment.summary_id == segment_summary_id)
            .count()
        )
        if count > 0:
            return False

    if epoch_checkpoint_id:
        count = (
            session.query(Epoch)
            .filter(Epoch.checkpoint_id == epoch_checkpoint_id)
            .count()
        )
        if count > 0:
            return False

    return True


def is_not_active_building_generation(generation_status: str) -> bool:
    """不是 active 或 building 的 retrieval generation。

    返回 True 表示安全可清理（是 retired/failed）。
    """
    return generation_status in ("retired", "failed")


def no_running_rebuild_or_refresh(session: Session) -> bool:
    """无未完成的 rebuild / refresh Job。"""
    from aiive.db.models import RetrievalIndexRun

    running = (
        session.query(RetrievalIndexRun)
        .filter(RetrievalIndexRun.status == "running")
        .count()
    )
    return running == 0


def is_forget_operation_clearable(
    session: Session,
    forget_operation_id: str,
) -> bool:
    """ForgetOperation 已到可清理终态：status='purged' 且 ForgetVerifier 已通过。

    返回 True 表示其子记录（Action/Batch/Dependency/Target）可按期清理。
    以下状态全部视为受保护、不可清理：
    requested / shielded / cascading / verifying / purge_ready / purging
    / failed_retryable / deadletter / shielded_deadletter
    """
    from aiive.db.forget_models import ForgetOperation, FORGET_CLEANUP_READY_STATUSES

    op = session.get(ForgetOperation, forget_operation_id)
    if op is None:
        return False
    # 已成功完成验证的终态（含三态 verified / verified_with_coarse_purge /
    # legacy_unverifiable，以及旧代码的 purged）方可清理。
    if op.status not in FORGET_CLEANUP_READY_STATUSES:
        return False
    # Verifier 必须已通过（verified_at IS NOT NULL）
    if op.verified_at is None:
        return False
    return True


# 受保护的 ForgetOperation 状态集合
PROTECTED_FORGET_STATUSES: frozenset[str] = frozenset({
    "requested", "shielded", "cascading", "verifying",
    "purge_ready", "purging", "failed_retryable",
    "deadletter", "shielded_deadletter",
})


def has_pending_outbox_jobs(session: Session, table_name: str, row_id: str) -> bool:
    """无 pending OutboxJob 引用该对象。"""
    from sqlalchemy import String, cast

    from aiive.db.models import OutboxJob

    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import JSONB

        pending = (
            session.query(OutboxJob)
            .filter(
                OutboxJob.status.in_(["pending", "running"]),
                # payload 是 JSON 列，PostgreSQL 的 json 不支持 @> 包含运算，
                # 需先 cast 成 jsonb（直接用 .contains 会退化成 LIKE 报
                # 「operator does not exist: json ~~ text」）。
                cast(OutboxJob.payload, JSONB).contains(
                    {"target_table": table_name, "target_id": row_id}
                ),
            )
            .count()
        )
        return pending > 0

    # SQLite 等其他方言无 JSONB：LIKE 预筛后 Python 精确判定。
    candidates = (
        session.query(OutboxJob)
        .filter(
            OutboxJob.status.in_(["pending", "running"]),
            cast(OutboxJob.payload, String).like(f"%{row_id}%"),
        )
        .all()
    )
    for job in candidates:
        payload = job.payload or {}
        if (
            payload.get("target_table") == table_name
            and payload.get("target_id") == row_id
        ):
            return True
    return False


def is_beyond_retention(cutoff_at: datetime, record_time: datetime | None) -> bool:
    """超过保留期。"""
    if record_time is None:
        return True
    return record_time <= cutoff_at


def run_all_safety_predicates(
    session: Session,
    cutoff_at: datetime,
    record_time: datetime | None = None,
    generation_status: str | None = None,
    segment_summary_id: str | None = None,
    epoch_checkpoint_id: str | None = None,
    forget_operation_id: str | None = None,
    table_name: str | None = None,
    row_id: str | None = None,
) -> tuple[bool, str]:
    """运行全部安全谓词，返回 (通过, 失败原因)。

    任一谓词不通过则返回 (False, 原因)。
    """
    if not is_beyond_retention(cutoff_at, record_time):
        return False, "未超过保留期"

    if generation_status is not None and not is_not_active_building_generation(generation_status):
        return False, "generation 状态为 active/building，不可清理"

    if segment_summary_id and not is_not_current_summary_or_checkpoint(
        session, segment_summary_id=segment_summary_id,
    ):
        return False, "是当前 Segment.summary_id 指针"

    if epoch_checkpoint_id and not is_not_current_summary_or_checkpoint(
        session, epoch_checkpoint_id=epoch_checkpoint_id,
    ):
        return False, "是当前 Epoch.checkpoint_id 指针"

    if not no_running_rebuild_or_refresh(session):
        return False, "存在未完成的 rebuild/refresh Job"

    if forget_operation_id and not is_forget_operation_clearable(
        session, forget_operation_id,
    ):
        return False, "ForgetOperation 未达可清理终态"

    if table_name and row_id and has_pending_outbox_jobs(session, table_name, row_id):
        return False, "存在 pending OutboxJob 引用"

    return True, ""
