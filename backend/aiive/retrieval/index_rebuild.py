"""Phase 5：检索索引全量重建（retrieval_index_rebuild 消费端）。

有界分批 + CONTINUE 分页：每批处理一个 source_type 的一个 id 游标批次，写 building
generation；三 type 穷尽后原子激活 building generation 并 retired 旧 active。

并发隔离（revision 3）：
- rebuild 写 building generation，refresh 同时写 active+building；
- upsert_entry 按 source_version fencing 保证旧 Job 不覆盖新数据；
- 激活前重新核对 active generation 的 version，确保只切换本 rebuild 创建的
  building generation。
"""
from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.base import SessionLocal
from aiive.db.models import (
    OutboxJob,
    RetrievalIndexGeneration,
    RetrievalIndexRun,
)
from aiive.memory.recall_config import RetrievalConfig
from aiive.retrieval.retrieval_index import RetrievalIndexManager
from aiive.retrieval.retrieval_store import build_entry_fields
from aiive.worker.outbox_dto import (
    ClaimedJob,
    HandlerOutcome,
    HandlerResult,
    NonRetryableJobError,
)

logger = logging.getLogger(__name__)

_BATCH_LIMIT = 200
_SOURCE_ORDER = ("memory_record", "segment_summary", "epoch_checkpoint")


# ── 可移植工具（避免与 outbox_handlers 形成循环导入）──

def _claim_matches(outbox: OutboxJob, claimed: ClaimedJob, now: datetime) -> bool:
    lease = outbox.lease_expires_at
    if lease is None:
        return False
    if lease.tzinfo is None:
        lease = lease.replace(tzinfo=timezone.utc)
    return (
        outbox.claim_token == claimed.claim_token
        and outbox.locked_by == claimed.worker_id
        and outbox.status == "running"
        and lease > now
    )


def _insert_conflict_do_nothing(
    db: Session, model: Any, values: dict[str, Any], conflict_columns: list[str],
) -> None:
    """幂等插入：冲突时忽略，不伤害外层事务。"""
    bind = db.get_bind()
    if getattr(bind.dialect, "name", "") == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = pg_insert(model).values(values).on_conflict_do_nothing(
            index_elements=conflict_columns,
        )
        db.execute(stmt)
        return
    # SQLite：先查再插，避免 IntegrityError 回滚外层事务
    existing = db.query(model).filter_by(
        **{col: values[col] for col in conflict_columns if col in values}
    ).first()
    if existing is not None:
        return
    # 用 SAVEPOINT 包裹单次插入：冲突时仅回滚本条，不中止整个 rebuild 事务
    sp = db.begin_nested()
    db.add(model(**values))
    try:
        db.flush()
        sp.commit()
    except Exception:
        sp.rollback()
        logger.warning("retrieval_index_rebuild 幂等插入冲突（单条跳过）: %s", conflict_columns)


# ── 主入口 ──

def handle_retrieval_index_rebuild(claimed: ClaimedJob) -> HandlerResult:
    """retrieval_index_rebuild 三阶段 Handler（有界分批 + CONTINUE）。"""
    payload = claimed.payload
    operation_id: str = payload.get("operation_id", "")

    db_a = SessionLocal()
    try:
        outbox_a = db_a.query(OutboxJob).filter(
            OutboxJob.id == claimed.id
        ).with_for_update().first()
        if outbox_a is None:
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "outbox_not_found")
        now = datetime.now(timezone.utc)
        if not _claim_matches(outbox_a, claimed, now):
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "claim_expired_or_taken")

        decision, run = _resolve_run(db_a, claimed, operation_id, now)
        if decision == "already_succeeded":
            db_a.rollback()
            return HandlerResult(HandlerOutcome.COMPLETED, "already_succeeded")
        if decision == "deadletter":
            db_a.rollback()
            return HandlerResult(
                HandlerOutcome.NON_RETRYABLE, "run_deadletter",
                terminal_reason="rebuild_run_deadletter",
            )
        if decision == "busy":
            db_a.rollback()
            return HandlerResult(HandlerOutcome.RETRY_LATER, "run_busy")

        gen = _ensure_building_generation(db_a, run, now)
        db_a.commit()
        run_id = run.id
        _index_version = gen.index_version
    except NonRetryableJobError as e:
        db_a.rollback()
        return HandlerResult(HandlerOutcome.NON_RETRYABLE, str(e), terminal_reason="phase_a")
    except Exception as e:
        db_a.rollback()
        logger.exception("retrieval_index_rebuild Phase A failed")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"Phase A failed: {e}")
    finally:
        db_a.close()

    db_b = SessionLocal()
    try:
        outbox_b = db_b.query(OutboxJob).filter(
            OutboxJob.id == claimed.id
        ).with_for_update().first()
        if outbox_b is None or not _claim_matches(outbox_b, claimed, datetime.now(timezone.utc)):
            db_b.rollback()
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "claim_lost_phase_b")

        run_b = db_b.query(RetrievalIndexRun).filter(
            RetrievalIndexRun.id == run_id
        ).with_for_update().first()
        if run_b is None or run_b.execution_token != claimed.claim_token:
            db_b.rollback()
            return HandlerResult(HandlerOutcome.CLAIM_LOST, "run_token_mismatch")
        if run_b.status != "running":
            db_b.rollback()
            if run_b.status == "succeeded":
                return HandlerResult(HandlerOutcome.COMPLETED, "already_succeeded")
            return HandlerResult(HandlerOutcome.CLAIM_LOST, f"run_not_running:{run_b.status}")

        gen_b = db_b.query(RetrievalIndexGeneration).filter(
            RetrievalIndexGeneration.index_version == run_b.index_version,
        ).with_for_update().first()
        if gen_b is None:
            db_b.rollback()
            return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, "generation_not_found")

        more = _process_one_batch(db_b, run_b, gen_b, now)
        if more:
            db_b.commit()
            return HandlerResult(HandlerOutcome.CONTINUE, "batch_continue")

        _activate_if_still_valid(db_b, gen_b, now)
        run_b.status = "succeeded"
        run_b.completed_at = now
        db_b.commit()
        return HandlerResult(HandlerOutcome.COMPLETED, "rebuild_succeeded")
    except NonRetryableJobError as e:
        db_b.rollback()
        _mark_run_failed(run_id, claimed.claim_token, str(e))
        return HandlerResult(HandlerOutcome.NON_RETRYABLE, str(e), terminal_reason="phase_b")
    except Exception as e:
        db_b.rollback()
        _mark_run_failed(run_id, claimed.claim_token, f"Phase B failed: {e}")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"Phase B failed: {e}")
    finally:
        db_b.close()


# ── Phase A：Run 解析 + 建 building generation ──

def _resolve_run(
    db: Session, claimed: ClaimedJob, operation_id: str, now: datetime,
) -> tuple[str, RetrievalIndexRun]:
    _insert_conflict_do_nothing(
        db, RetrievalIndexRun,
        {
            "id": str(_uuid.uuid4()),
            "outbox_job_id": claimed.id,
            "operation_id": operation_id,
            "status": "running",
            "index_version": 0,  # 占位，_ensure_building_generation 会覆盖为真值
            "batch_cursor": {"source_type": _SOURCE_ORDER[0], "last_id": None},
        },
        conflict_columns=["outbox_job_id"],
    )
    run = db.query(RetrievalIndexRun).filter(
        RetrievalIndexRun.outbox_job_id == claimed.id,
    ).with_for_update().first()
    if run is None:
        raise NonRetryableJobError("无法解析 rebuild Run")
    if run.status == "succeeded":
        return "already_succeeded", run
    if run.status == "deadletter":
        return "deadletter", run
    if run.status == "running" and run.execution_token:
        # 同 claim_token 重入（CONTINUE 循环）→ 不视为 busy
        if run.execution_token != claimed.claim_token:
            ob = db.query(OutboxJob).filter(
                OutboxJob.claim_token == run.execution_token,
                OutboxJob.status == "running",
            ).first()
            if ob and ob.lease_expires_at:
                lease = ob.lease_expires_at
                if lease.tzinfo is None:
                    lease = lease.replace(tzinfo=timezone.utc)
                if lease > now:
                    return "busy", run
        was_running = True
    else:
        was_running = False
    run.status = "running"
    run.execution_token = claimed.claim_token
    run.attempt_count = (run.attempt_count or 0) + 1
    run.started_at = now
    if was_running:
        run.completed_at = None
        run.error_message = None
    db.flush()
    return ("acquired" if not was_running else "takeover"), run


def _ensure_building_generation(
    db: Session, run: RetrievalIndexRun, _now: datetime,
) -> RetrievalIndexGeneration:
    if run.index_version:
        existing = db.query(RetrievalIndexGeneration).filter(
            RetrievalIndexGeneration.index_version == run.index_version,
        ).first()
        if existing is not None:
            return existing
    mgr = RetrievalIndexManager()
    iv = mgr.next_index_version(db)
    gen = mgr.create_generation(
        db, iv, status="building",
        policy_version=RetrievalConfig().policy_version,
    )
    run.index_version = iv
    db.flush()
    return gen


# ── Phase B：有界分批 ──

def _process_one_batch(
    db: Session, run: RetrievalIndexRun, gen: RetrievalIndexGeneration, _now: datetime,
) -> bool:
    """处理一个 id 游标批次；返回是否还有更多批次待处理。"""
    mgr = RetrievalIndexManager()
    cursor: dict[str, Any] = run.batch_cursor or {"source_type": _SOURCE_ORDER[0], "last_id": None}
    source_type: str = cursor["source_type"]
    last_id: str | None = cursor.get("last_id")  # type: ignore[assignment]

    batch = mgr.next_source_batch(db, source_type, last_id, _BATCH_LIMIT)
    if batch:
        for orm in batch:
            fields = build_entry_fields(db, source_type, orm)
            is_invalid_memory = (
                fields is not None
                and fields.get("source_type") == "memory_record"
                and (fields.get("validity_state") or "valid")
                in ("superseded", "contradicted", "expired")
            )
            if fields is None or fields.get("is_forgotten") or is_invalid_memory:
                # forgotten / validity 非 valid 源：在该 generation 内清理其
                # Entry（清空文本/不检索），而非保留旧 active 的 current Entry。
                mgr.tombstone_by_source(
                    db, gen.index_version, source_type,
                    source_id=fields["source_id"] if fields else orm.id,
                )
                run.skipped_stale_count = (run.skipped_stale_count or 0) + 1
                continue
            mgr.upsert_entry(
                db, gen,
                source_type=fields["source_type"],
                source_id=fields["source_id"],
                source_version=fields["source_version"],
                source_hash=fields.get("source_hash"),
                title=fields["title"],
                search_text=fields["search_text"],
                snippet=fields["snippet"],
                scope_type=fields["scope_type"],
                scope_id=fields["scope_id"],
                canonical_key=fields.get("canonical_key"),
                lifecycle_state=fields.get("lifecycle_state"),
                validity_state=fields.get("validity_state"),
                retrieval_tier=fields["retrieval_tier"],
                thread_id=fields.get("thread_id"),
                epoch_id=fields.get("epoch_id"),
                segment_id=fields.get("segment_id"),
                memory_record_id=fields.get("memory_record_id"),
                metadata=fields.get("metadata") or {},
                created_source_at=fields.get("created_source_at"),
                updated_source_at=fields.get("updated_source_at"),
                tokens=fields["tokens"],
            )
        new_last = batch[-1].id
        run.indexed_count = (run.indexed_count or 0) + len(batch)
        db.flush()
        # 当前 source 是否还有剩余
        nxt = mgr.next_source_batch(db, source_type, new_last, 1)
        if nxt:
            run.batch_cursor = {"source_type": source_type, "last_id": new_last}
            return True
        return _advance_source(db, run, source_type)

    return _advance_source(db, run, source_type)


def _advance_source(db: Session, run: RetrievalIndexRun, source_type: str) -> bool:
    idx = _SOURCE_ORDER.index(source_type)
    if idx + 1 < len(_SOURCE_ORDER):
        run.batch_cursor = {"source_type": _SOURCE_ORDER[idx + 1], "last_id": None}
        db.flush()
        return True
    return False


def _activate_if_still_valid(
    db: Session, gen: RetrievalIndexGeneration, _now: datetime,
) -> None:
    """激活 building generation，但先核对旧 active 版本未被意外替换（revision 3）。"""
    mgr = RetrievalIndexManager()
    active = mgr.get_active_generation(db)
    if active is not None and active.index_version == gen.index_version:
        return  # 已激活（可能此前被接管后激活）
    if active is not None and active.index_version > gen.index_version:
        # #18：当前 active 已是更新的 generation（并发 rebuild 已切换）——
        # 不得把旧 generation 重新激活、retire 更新的 active。
        # 本 generation 直接 retire，交由 retention 清理。
        logger.warning(
            "放弃激活旧 generation v%s（active 已是 v%s）",
            gen.index_version, active.index_version,
        )
        gen.status = "retired"
        gen.status_changed_at = _now
        db.flush()
        return
    mgr.activate_generation(db, gen.index_version)
    # 该 generation 已通过全量 backfill 构建完成
    gen.backfill_done = True
    db.flush()


def _mark_run_failed(run_id: str, execution_token: str, error: str) -> None:
    db_m = SessionLocal()
    try:
        affected = db_m.query(RetrievalIndexRun).filter(
            RetrievalIndexRun.id == run_id,
            RetrievalIndexRun.execution_token == execution_token,
            RetrievalIndexRun.status == "running",
        ).update({
            RetrievalIndexRun.status: "failed",
            RetrievalIndexRun.execution_token: None,
            RetrievalIndexRun.error_message: error[:500],
            RetrievalIndexRun.completed_at: datetime.now(timezone.utc),
            # 真正失败重试一次即累计一次（CONTINUE 分页不调用本函数，故不计入）。
            RetrievalIndexRun.failure_attempt_count: (
                RetrievalIndexRun.failure_attempt_count + 1
            ),
        }, synchronize_session=False)
        db_m.commit()
        if affected == 0:
            logger.warning("_mark_run_failed: 无匹配行 run_id=%s", run_id)
    except Exception:
        logger.exception("_mark_run_failed 自身失败")
    finally:
        db_m.close()
