"""启动时幂等补齐历史记忆的向量投影任务。"""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy import or_
from sqlalchemy.orm import Session

from aiive.config import settings
from aiive.db.base import SessionLocal
from aiive.db.models import MemoryRecord, MemoryVectorProjection, OutboxJob
from aiive.memory.memory_types import LifecycleState, ValidityState
from aiive.memory.vector_projection import embedding_model_identity

logger = logging.getLogger(__name__)


def _embedding_generation() -> str:
    raw = "|".join((
        settings.aiive_embedding_provider,
        embedding_model_identity(),
        str(settings.aiive_embedding_dimensions),
        settings.aiive_embedding_query_prefix,
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def ensure_memory_vector_backfill() -> int:
    """为缺失或过期的 active+valid 记忆幂等入队向量刷新任务。"""
    if not settings.aiive_memory_vector_enabled:
        return 0
    db = SessionLocal()
    try:
        count = _ensure_memory_vector_backfill_in_session(db)
        db.commit()
        if count:
            logger.info("已入队 %d 条历史记忆向量回填任务", count)
        return count
    except Exception:
        db.rollback()
        logger.exception("历史记忆向量回填 bootstrap 失败")
        raise
    finally:
        db.close()


def _ensure_memory_vector_backfill_in_session(db: Session) -> int:
    identity = embedding_model_identity()
    rows = (
        db.query(MemoryRecord.id, MemoryRecord.record_version)
        .outerjoin(
            MemoryVectorProjection,
            MemoryVectorProjection.memory_id == MemoryRecord.id,
        )
        .filter(
            MemoryRecord.lifecycle_state == LifecycleState.ACTIVE.value,
            MemoryRecord.validity_state == ValidityState.VALID.value,
            or_(
                MemoryVectorProjection.memory_id.is_(None),
                MemoryVectorProjection.record_version != MemoryRecord.record_version,
                MemoryVectorProjection.content_hash.is_distinct_from(
                    MemoryRecord.content_hash,
                ),
                MemoryVectorProjection.embedding_model != identity,
            ),
        )
        .all()
    )
    generation = _embedding_generation()
    inserted = 0
    for memory_id, record_version in rows:
        operation_id = (
            f"memory_vector_backfill:{generation}:{memory_id}:{record_version}"
        )
        exists = db.query(OutboxJob.id).filter_by(operation_id=operation_id).first()
        if exists is not None:
            continue
        db.add(OutboxJob(
            operation_id=operation_id,
            job_type="memory_vector_refresh",
            status="pending",
            payload={
                "schema_version": 1,
                "memory_id": memory_id,
                "record_version": record_version,
                "embedding_generation": generation,
            },
            trace_id=memory_id,
            max_retries=3,
        ))
        inserted += 1
    return inserted
