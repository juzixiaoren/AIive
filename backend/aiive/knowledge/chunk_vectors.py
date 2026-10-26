"""知识分块向量的生成、持久化、清理与 pgvector 检索。

设计约束：
- 向量是 `chunks` 的**派生数据**，永远可由分块内容重建，不作为事实来源；
- 检索命中后仍回源读取 `chunks` / `documents`，失效文档（superseded / deleted）
  即使残留向量也不会被召回；
- Embedding 不可用时抛 `EmbeddingProviderError`，由调用方降级为纯关键词检索，
  绝不返回未经校验的伪造结果。
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from typing import Any, cast

from sqlalchemy import delete, or_, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from aiive.config import settings
from aiive.db.models import Chunk, Document, KnowledgeChunkVector
from aiive.knowledge.ingestor import INACTIVE_STATUSES
from aiive.memory.embedding_provider import OpenAIEmbeddingProvider
from aiive.memory.vector_projection import (
    build_embedding_provider,
    embedding_model_identity,
)

logger = logging.getLogger(__name__)

EmbeddingFactory = Callable[[], OpenAIEmbeddingProvider]


def chunk_content_hash(content: str) -> str:
    """返回分块内容的稳定哈希，用于判断投影是否过期。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def knowledge_vector_enabled(db: Session) -> bool:
    """当前配置与数据库方言是否支持知识分块向量。

    SQLite（离线测试/轻量部署）没有 pgvector，向量通道整体关闭，
    检索退回纯关键词路径，行为与未接入时一致。
    """
    if not settings.aiive_knowledge_vector_enabled:
        return False
    bind = db.bind
    return bind is not None and bind.dialect.name == "postgresql"


def vector_table_ready(db: Session) -> bool:
    """确认向量开关已开启且投影表存在（迁移未执行时降级为关键词检索）。"""
    if not knowledge_vector_enabled(db):
        return False
    ready = db.execute(
        text("SELECT to_regclass('public.knowledge_chunk_vectors') IS NOT NULL")
    ).scalar()
    return bool(ready)


class ChunkVectorService:
    """维护知识分块向量投影，并提供向量召回。"""

    def __init__(
        self,
        db: Session,
        provider_factory: EmbeddingFactory = build_embedding_provider,
    ) -> None:
        self._db: Session = db
        self._provider_factory: EmbeddingFactory = provider_factory

    # ── 写入 / 清理 ─────────────────────────────────────────────

    def refresh_document(self, document_id: str) -> int:
        """为指定文档的当前分块幂等生成/刷新向量，并清理多余投影。

        分块内容未变且模型身份一致时跳过重算，避免重建文档时重复调用模型。
        """
        chunks = (
            self._db.query(Chunk)
            .filter(Chunk.document_id == document_id)
            .order_by(Chunk.chunk_index.asc())
            .all()
        )
        if not chunks:
            self.delete_document(document_id)
            return 0
        identity = embedding_model_identity()
        existing = {
            row.chunk_id: row
            for row in self._db.query(KnowledgeChunkVector)
            .filter(KnowledgeChunkVector.chunk_id.in_([chunk.id for chunk in chunks]))
            .all()
        }
        pending = [
            chunk
            for chunk in chunks
            if not self._projection_fresh(existing.get(chunk.id), chunk, identity)
        ]
        if pending:
            vectors = self._provider_factory().embed_batch(
                [chunk.content for chunk in pending],
                input_type="document",
                batch_size=settings.aiive_embedding_batch_size,
            )
            for chunk, vector in zip(pending, vectors, strict=True):
                row = existing.get(chunk.id)
                if row is None:
                    row = KnowledgeChunkVector(chunk_id=chunk.id)
                    self._db.add(row)
                row.content_hash = chunk_content_hash(chunk.content)
                row.embedding_model = identity
                row.embedding = vector
        keep_ids = {chunk.id for chunk in chunks}
        stale_ids = [chunk_id for chunk_id in existing if chunk_id not in keep_ids]
        if stale_ids:
            self._db.execute(
                delete(KnowledgeChunkVector).where(KnowledgeChunkVector.chunk_id.in_(stale_ids))
            )
        self._db.flush()
        return len(chunks)

    def delete_document(self, document_id: str) -> int:
        """删除文档全部分块对应的向量投影。"""
        chunk_ids = self._db.query(Chunk.id).filter(Chunk.document_id == document_id)
        result = cast(CursorResult[Any], self._db.execute(
            delete(KnowledgeChunkVector).where(KnowledgeChunkVector.chunk_id.in_(chunk_ids))
        ))
        self._db.flush()
        return int(result.rowcount or 0)

    def backfill_missing(self, limit: int) -> tuple[int, int]:
        """为缺失/过期投影的活跃文档补建向量，返回 (成功文档数, 失败文档数)。

        每篇文档独立保存点：单篇失败只影响该篇，且不会把失败当成功。
        """
        document_ids = missing_vector_document_ids(self._db, limit)
        done = 0
        failed = 0
        for document_id in document_ids:
            try:
                with self._db.begin_nested():
                    self.refresh_document(document_id)
                done += 1
            except Exception:
                failed += 1
                logger.warning("知识分块向量回填失败：%s", document_id, exc_info=True)
        return done, failed

    # ── 检索 ────────────────────────────────────────────────────

    def search(self, query: str, limit: int) -> list[tuple[Chunk, Document, float]]:
        """按余弦相似度召回活跃文档的分块，返回 (chunk, document, score)。

        score 为 1 - cosine_distance，取值 0..1；只使用与当前模型身份一致的投影。
        """
        normalized = query.strip()
        if not normalized or limit <= 0:
            return []
        vector = self._provider_factory().embed(normalized, input_type="query")
        distance = KnowledgeChunkVector.embedding.cosine_distance(vector)
        rows = (
            self._db.query(Chunk, Document, distance.label("distance"))
            .join(KnowledgeChunkVector, KnowledgeChunkVector.chunk_id == Chunk.id)
            .join(Document, Chunk.document_id == Document.id)
            .filter(
                Document.status.notin_(tuple(INACTIVE_STATUSES)),
                KnowledgeChunkVector.embedding_model == embedding_model_identity(),
            )
            .order_by(distance.asc())
            .limit(limit)
            .all()
        )
        return [
            (chunk, document, max(0.0, min(1.0, 1.0 - float(raw_distance))))
            for chunk, document, raw_distance in rows
        ]

    @staticmethod
    def _projection_fresh(
        row: KnowledgeChunkVector | None,
        chunk: Chunk,
        identity: str,
    ) -> bool:
        """投影是否仍然对应当前分块内容与模型身份。"""
        return (
            row is not None
            and row.embedding_model == identity
            and row.content_hash == chunk_content_hash(chunk.content)
        )


def _stale_chunk_id_query(db: Session):
    """返回需要清理的向量 chunk_id 子查询：孤儿分块或所属文档已失效。"""
    orphan_chunk = (
        db.query(KnowledgeChunkVector.chunk_id)
        .outerjoin(Chunk, Chunk.id == KnowledgeChunkVector.chunk_id)
        .filter(Chunk.id.is_(None))
    )
    inactive_chunk = (
        db.query(KnowledgeChunkVector.chunk_id)
        .join(Chunk, Chunk.id == KnowledgeChunkVector.chunk_id)
        .join(Document, Chunk.document_id == Document.id)
        .filter(Document.status.in_(tuple(INACTIVE_STATUSES)))
    )
    return orphan_chunk.union(inactive_chunk)


def missing_vector_document_ids(db: Session, limit: int) -> list[str]:
    """返回存在缺失/过期分块向量的活跃文档 id（有界批次）。"""
    identity = embedding_model_identity()
    missing_chunk = (
        db.query(Chunk.document_id)
        .outerjoin(KnowledgeChunkVector, KnowledgeChunkVector.chunk_id == Chunk.id)
        .filter(
            or_(
                KnowledgeChunkVector.chunk_id.is_(None),
                KnowledgeChunkVector.embedding_model != identity,
            )
        )
        .subquery()
    )
    rows = (
        db.query(Document.id)
        .join(Chunk, Chunk.document_id == Document.id)
        .filter(
            Document.status.notin_(tuple(INACTIVE_STATUSES)),
            Document.id.in_(db.query(missing_chunk.c.document_id)),
        )
        .distinct()
        .limit(limit)
        .all()
    )
    return [str(row[0]) for row in rows]


def orphan_vector_count(db: Session) -> int:
    """返回孤儿向量与失效文档向量的条数（只读扫描）。"""
    stale = _stale_chunk_id_query(db).subquery()
    return int(
        db.query(KnowledgeChunkVector)
        .filter(KnowledgeChunkVector.chunk_id.in_(db.query(stale.c[0])))
        .count()
    )


def purge_orphan_vectors(db: Session, limit: int) -> int:
    """删除孤儿/失效向量投影（幂等，可重复执行）。"""
    stale = _stale_chunk_id_query(db).limit(limit).subquery()
    ids = [str(row[0]) for row in db.query(stale.c[0]).all()]
    if not ids:
        return 0
    result = cast(CursorResult[Any], db.execute(
        delete(KnowledgeChunkVector).where(KnowledgeChunkVector.chunk_id.in_(ids))
    ))
    db.flush()
    return int(result.rowcount or 0)
