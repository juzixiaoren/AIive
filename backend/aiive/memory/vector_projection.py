"""记忆 pgvector 投影的写入、删除与 query-aware 查询。"""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import and_, or_, text
from sqlalchemy.orm import Session

from aiive.config import settings
from aiive.db.models import MemoryRecord, MemoryVectorProjection
from aiive.forget.visibility_service import ForgetVisibilityService
from aiive.memory.embedding_provider import OpenAIEmbeddingProvider
from aiive.memory.memory_types import LifecycleState, ValidityState
from aiive.memory.recall_models import MemoryRecallRequest


EmbeddingFactory = Callable[[], OpenAIEmbeddingProvider]


def validate_vector_runtime(db: Session) -> None:
    """显式启用时验证 PostgreSQL、vector 扩展和投影表均已就绪。"""
    if not settings.aiive_memory_vector_enabled:
        return
    if db.bind is None or db.bind.dialect.name != "postgresql":
        raise RuntimeError("记忆向量能力仅支持 PostgreSQL + pgvector")
    extension_ready = db.execute(text(
        "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"
    )).scalar()
    table_ready = db.execute(text(
        "SELECT to_regclass('public.memory_vector_projections') IS NOT NULL"
    )).scalar()
    if not extension_ready or not table_ready:
        raise RuntimeError("pgvector 扩展或 memory_vector_projections 表未就绪")
    build_embedding_provider()


def build_embedding_provider() -> OpenAIEmbeddingProvider:
    """按当前运行配置构造真实 OpenAI Embeddings provider。"""
    return OpenAIEmbeddingProvider(
        api_key=settings.aiive_embedding_api_key,
        base_url=settings.aiive_embedding_base_url,
        model=settings.aiive_embedding_model,
        dimensions=settings.aiive_embedding_dimensions,
        timeout_seconds=settings.aiive_embedding_timeout_seconds,
    )


class MemoryVectorProjectionService:
    """维护可重建的向量投影，并在查询后执行真相源回源校验。"""

    def __init__(
        self,
        db: Session,
        provider_factory: EmbeddingFactory = build_embedding_provider,
    ) -> None:
        self._db = db
        self._provider_factory = provider_factory

    def refresh(self, memory_id: str, expected_version: int) -> str:
        """按当前 MemoryRecord 状态幂等 upsert 或删除投影。"""
        record = self._db.get(MemoryRecord, memory_id)
        if record is None:
            self._db.query(MemoryVectorProjection).filter_by(memory_id=memory_id).delete()
            return "source_missing_deleted"
        if record.record_version != expected_version:
            return "stale_version_skipped"
        if not self._is_indexable(record):
            self._db.query(MemoryVectorProjection).filter_by(memory_id=memory_id).delete()
            return "inactive_deleted"

        provider = self._provider_factory()
        vector = provider.embed(self._embedding_text(record))
        projection = self._db.get(MemoryVectorProjection, memory_id)
        if projection is None:
            projection = MemoryVectorProjection(memory_id=memory_id)
            self._db.add(projection)
        projection.record_version = record.record_version
        projection.content_hash = record.content_hash
        projection.embedding_model = settings.aiive_embedding_model
        projection.embedding = vector
        return "upserted"

    def delete(self, memory_id: str, expected_version: int) -> str:
        """删除不高于当前事件版本的投影，防止旧删除任务移除新版本。"""
        projection = self._db.get(MemoryVectorProjection, memory_id)
        if projection is None:
            return "already_absent"
        if projection.record_version > expected_version:
            return "stale_delete_skipped"
        self._db.delete(projection)
        return "deleted"

    def search(
        self,
        request: MemoryRecallRequest,
        *,
        include_sleeping: bool,
        include_archived: bool,
        limit: int,
    ) -> list[tuple[MemoryRecord, float]]:
        """向量搜索后按 scope、生命周期、版本与 Forget 状态 fail-closed 回源。"""
        query = request.query.strip()
        if not query or limit <= 0:
            return []
        vector = self._provider_factory().embed(query)
        distance = MemoryVectorProjection.embedding.cosine_distance(vector)
        states = [LifecycleState.ACTIVE.value]
        if include_sleeping:
            states.append(LifecycleState.SLEEPING.value)
        if include_archived:
            states.append(LifecycleState.ARCHIVED.value)

        scope_conditions = []
        for scope_type, scope_id in request.scope_context.chain():
            scope_conditions.append(and_(
                MemoryRecord.scope_type == scope_type,
                MemoryRecord.scope_id == scope_id if scope_id is not None else MemoryRecord.scope_id.is_(None),
            ))
        rows = (
            self._db.query(MemoryRecord, distance.label("distance"))
            .join(MemoryVectorProjection, MemoryVectorProjection.memory_id == MemoryRecord.id)
            .filter(
                MemoryRecord.lifecycle_state.in_(states),
                MemoryRecord.validity_state == ValidityState.VALID.value,
                MemoryVectorProjection.record_version == MemoryRecord.record_version,
                or_(*scope_conditions),
            )
            .order_by(distance.asc())
            .limit(limit)
            .all()
        )
        allowed_ids = set(ForgetVisibilityService.filter_memory_ids(
            self._db, [record.id for record, _ in rows],
        ))
        return [
            (record, max(0.0, min(1.0, 1.0 - float(raw_distance))))
            for record, raw_distance in rows
            if record.id in allowed_ids
        ]

    @staticmethod
    def _is_indexable(record: MemoryRecord) -> bool:
        return (
            record.lifecycle_state == LifecycleState.ACTIVE.value
            and record.validity_state == ValidityState.VALID.value
        )

    @staticmethod
    def _embedding_text(record: MemoryRecord) -> str:
        key = record.canonical_key or ""
        return f"{key}\n{record.content}" if key else record.content
