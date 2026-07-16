"""Phase 5：历史 backfill 生产级 bootstrap（revision R）。

升级前已存在的 MemoryRecord / SegmentSummary / EpochCheckpoint 不会自动进入
统一索引（新数据靠写入时 refresh 流入）。本模块在应用启动（_ensure_retrieval_generation
之后）幂等地入队一次 retrieval_index_rebuild，将该 generation 的全量源回填进索引。

设计要点：
- 稳定 operation_id：retrieval_index_rebuild:bootstrap:{index_version}
  （OutboxJob.operation_id 有 UNIQUE 约束，天然幂等，重启可续跑）
- 已完成 backfill 的 active generation（backfill_done=True）不再重复入队
- 空库（无任何源数据）无需 rebuild，直接标记 backfill_done，避免无谓 generation 切换
- 不在 migration 中执行 backfill，不依赖用户调用工具
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from aiive.db.base import SessionLocal
from aiive.db.models import (
    EpochCheckpoint,
    MemoryRecord,
    OutboxJob,
    SegmentSummary,
)
from aiive.retrieval.retrieval_index import RetrievalIndexManager

logger = logging.getLogger(__name__)

_BOOTSTRAP_PREFIX = "retrieval_index_rebuild:bootstrap"


def ensure_retrieval_backfill() -> None:
    """幂等确保 active generation 已完成初始全量 backfill。

    由 main.lifespan 在 _ensure_retrieval_generation 之后调用。无 active generation
    时（_ensure_retrieval_generation 尚未创建）直接返回，交由后续逻辑处理。
    """
    db = SessionLocal()
    try:
        _ensure_backfill_in_session(db)
    except Exception:
        logger.exception("retrieval backfill bootstrap 失败")
        db.rollback()
    finally:
        db.close()


def _ensure_backfill_in_session(db: Session) -> None:
    mgr = RetrievalIndexManager()
    gen = mgr.get_active_generation(db)
    if gen is None:
        return
    if gen.backfill_done:
        return

    # 空库（升级前无任何源数据）无需 rebuild，直接标记已回填
    if not _has_any_source(db):
        gen.backfill_done = True
        db.commit()
        return

    op_id = f"{_BOOTSTRAP_PREFIX}:{gen.index_version}"
    existing = db.query(OutboxJob).filter_by(operation_id=op_id).first()
    if existing is not None:
        return  # 已入队（pending/running/succeeded），不重复
    db.add(OutboxJob(
        operation_id=op_id,
        job_type="retrieval_index_rebuild",
        status="pending",
        payload={"schema_version": 1, "operation_id": op_id, "bootstrap": True},
        trace_id="retrieval-bootstrap",
        max_retries=3,
    ))
    db.commit()
    logger.info("已入队 retrieval backfill: %s", op_id)


def _has_any_source(db: Session) -> bool:
    for model in (MemoryRecord, SegmentSummary, EpochCheckpoint):
        if db.query(model.id).first() is not None:
            return True
    return False
