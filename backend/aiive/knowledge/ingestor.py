"""知识文档摄取、生命周期管理与检索模块。

文档生命周期：
- ingest：按内容哈希去重；同一 source_path 内容变化时把旧版本标记 superseded，
  并立即清空旧分块，避免新旧内容同时被召回。
- delete：软删除，清空分块并释放对象存储原文；**永不删除 source_path 指向的用户文件**。
- reindex：从持久原文重建分块；失败时保留可恢复状态（index_failed）。
- search_chunks：只检索活跃状态文档的分块。
"""

import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from aiive.db.models import Chunk, Document
from aiive.knowledge.chunker import chunk_text
from aiive.knowledge.document_reader import extract_document_bytes
from aiive.storage.object_store import (
    ObjectRef,
    delete as delete_object,
    exists as object_exists,
    get_verified,
    put_content_addressed,
)

logger = logging.getLogger(__name__)
KNOWLEDGE_BUCKET = "knowledge-documents"

# 活跃状态：分块参与检索。
ACTIVE_STATUSES: frozenset[str] = frozenset({"indexed", "index_failed", "source_unavailable"})
# 可自动修复的活跃状态：原文对象应当仍可读取，允许重建分块
RECOVERABLE_STATUSES: frozenset[str] = frozenset({"indexed", "index_failed"})
# 失效状态：分块必须为空，且绝不参与检索。
INACTIVE_STATUSES: frozenset[str] = frozenset({"superseded", "deleted"})
DOCUMENT_STATUSES: frozenset[str] = ACTIVE_STATUSES | INACTIVE_STATUSES


def _now() -> datetime:
    """返回当前 UTC 时间。"""
    return datetime.now(timezone.utc)


def normalize_source_path(file_path: str) -> str:
    """把来源路径规范化为可比较的绝对路径字符串。"""
    return str(Path(file_path).resolve())


class KnowledgeIngestor:
    """知识文档摄取器和持久原文访问入口。"""

    def __init__(self, db: Session):
        """绑定当前数据库事务。"""
        self._db: Session = db

    # ── 分块与对象 ────────────────────────────────────────────────

    def _replace_chunks(self, document: Document, content: str) -> int:
        """在当前事务中用持久原文重建全部分块，并刷新分块向量。"""
        self._purge_chunk_vectors(document.id)
        self._db.query(Chunk).filter(Chunk.document_id == document.id).delete(
            synchronize_session=False
        )
        chunks = chunk_text(content)
        for item in chunks:
            self._db.add(Chunk(
                document_id=document.id,
                content=item["content"],
                line_start=item["line_start"],
                line_end=item["line_end"],
                chunk_index=item["chunk_index"],
                token_estimate=max(1, len(item["content"]) // 4),
            ))
        self._db.flush()
        self._refresh_chunk_vectors(document.id)
        return len(chunks)

    def purge_chunks(self, document_id: str) -> int:
        """删除指定文档的全部分块与派生向量（可由原文重建）。"""
        self._purge_chunk_vectors(document_id)
        return int(
            self._db.query(Chunk).filter(Chunk.document_id == document_id).delete(
                synchronize_session=False
            )
        )

    # ── 分块向量（派生数据，失败降级为纯关键词检索）────────────────

    def _refresh_chunk_vectors(self, document_id: str) -> None:
        """为当前分块生成向量；失败只告警，不影响摄取结果。

        向量通道不可用（未开启 / SQLite / 模型服务故障）时静默跳过：分块与
        关键词检索仍然有效，不会产生伪造或越权结果。
        """
        from aiive.knowledge.chunk_vectors import (
            ChunkVectorService,
            knowledge_vector_enabled,
        )

        if not knowledge_vector_enabled(self._db):
            return
        try:
            with self._db.begin_nested():
                ChunkVectorService(self._db).refresh_document(document_id)
        except Exception:
            logger.warning("知识分块向量生成失败，降级为关键词检索：%s", document_id, exc_info=True)

    def _purge_chunk_vectors(self, document_id: str) -> None:
        """删除文档分块对应的向量投影；失败只告警（残留由维护任务回收）。"""
        from aiive.knowledge.chunk_vectors import (
            ChunkVectorService,
            knowledge_vector_enabled,
        )

        if not knowledge_vector_enabled(self._db):
            return
        try:
            with self._db.begin_nested():
                ChunkVectorService(self._db).delete_document(document_id)
        except Exception:
            logger.warning("知识分块向量清理失败，留给维护任务回收：%s", document_id, exc_info=True)

    def release_object(self, document: Document) -> bool:
        """释放文档独占的对象存储原文，并清空其对象引用。

        仅当没有其他文档记录引用同一对象时才真正删除；删除失败只记录告警，
        遗留对象由维护任务的孤儿对象回收兜底（因此删除本身仍是幂等的）。
        """
        bucket = document.object_bucket
        key = document.object_key
        if not bucket or not key:
            return False
        shared = (
            self._db.query(Document)
            .filter(
                Document.object_bucket == bucket,
                Document.object_key == key,
                Document.id != document.id,
            )
            .count()
        )
        if shared:
            return False
        ref = ObjectRef(bucket, key)
        document.object_bucket = None
        document.object_key = None
        try:
            if not object_exists(ref):
                return False
            delete_object(ref)
            return True
        except Exception:
            logger.warning("知识原文对象释放失败，留给维护任务回收：%s", key, exc_info=True)
            return False

    # ── 版本管理 ──────────────────────────────────────────────────

    def _supersede(self, document: Document, by_id: str | None) -> dict[str, Any]:
        """把文档标记为被新版本取代，并立即清空其分块。"""
        removed = self.purge_chunks(document.id)
        document.status = "superseded"
        document.superseded_by = by_id
        document.updated_at = _now()
        return {
            "document_id": document.id,
            "source_path": document.source_path,
            "chunks_removed": removed,
        }

    def _supersede_other_versions(
        self, source_path: str, keep: Document | None
    ) -> list[dict[str, Any]]:
        """把同一来源路径的其他活跃版本标记为 superseded。

        `keep` 为需要保留的文档（允许是尚未 `add` 的瞬态对象——仅取其 id 参与
        过滤与版本链指向）。调用方必须"先置失效、再写入/激活保留文档"，以满足
        `ix_documents_active_source_path` 部分唯一索引的写序要求。
        """
        query = self._db.query(Document).filter(
            Document.source_path == source_path,
            Document.status.in_(tuple(ACTIVE_STATUSES)),
        )
        if keep is not None:
            query = query.filter(Document.id != keep.id)
        return [self._supersede(item, keep.id if keep else None) for item in query.all()]

    # ── 摄取 ──────────────────────────────────────────────────────

    def ingest(self, file_path: str) -> dict[str, Any]:
        """持久化文件原文，并创建、更新或复用文档及分块记录。

        同一 `source_path` 内容变化时，旧版本标记为 `superseded` 且分块被清空，
        因此检索只会命中最新版本；内容与历史版本一致时复用原文档记录。

        写序约束：`documents` 的 `ix_documents_active_source_path` 部分唯一索引
        要求同一 `source_path` 同时只有一条活跃文档，因此必须"先置失效旧版本并
        flush，再写入/复活新版本"；并发导入同路径时后提交方被索引拦截回滚，
        客户端重试即可收敛。
        """
        path = Path(file_path)
        if not path.is_file():
            return {"ok": False, "error": "文件不存在", "path": file_path}
        source_path = normalize_source_path(file_path)
        try:
            data = path.read_bytes()
            extracted = extract_document_bytes(data, path.name)
            mime_type = extracted.mime_type
            ref, content_hash = put_content_addressed(
                KNOWLEDGE_BUCKET, data, {"mime_type": mime_type, "source_name": path.name}
            )
            existing = self._db.query(Document).filter(
                Document.content_hash == content_hash
            ).first()
            if existing is not None:
                revived = existing.status in INACTIVE_STATUSES
                # 先把同路径其他活跃版本置失效并立即 flush（移除其部分唯一索引项），
                # 再让现有记录转为活跃：ix_documents_active_source_path 要求同一
                # source_path 同时只有一条活跃文档，不能依赖同一次 flush 内的顺序。
                superseded = self._supersede_other_versions(source_path, keep=existing)
                self._db.flush()
                if revived:
                    # 内容回到历史版本：复活该记录（content_hash 唯一，无法另建一行）
                    existing.status = "indexed"
                    existing.superseded_by = None
                    existing.deleted_at = None
                    existing.source_path = source_path
                    existing.title = path.name
                    existing.doc_type = extracted.doc_type
                if not existing.object_bucket or not existing.object_key:
                    existing.object_bucket = ref.bucket
                    existing.object_key = ref.key
                    existing.content_size = len(data)
                    existing.mime_type = mime_type
                    existing.status = "indexed"
                chunks = self._db.query(Chunk).filter(
                    Chunk.document_id == existing.id
                ).count()
                if revived or chunks == 0:
                    # 去重命中但分块缺失（此前摄取失败/被清理）→ 补建分块
                    chunks = self._replace_chunks(existing, extracted.text)
                    existing.status = "indexed"
                self._db.flush()
                return {
                    "ok": True, "duplicate": True, "document_id": existing.id,
                    "chunks": chunks, "content_hash": content_hash,
                    "revived": revived, "superseded": superseded,
                }

            # 新文档路径：先置失效同路径的其他活跃版本并 flush，再插入新版本。
            # 若顺序相反（先 insert 后 supersede），flush 时同一 source_path 会
            # 短暂出现两条活跃文档，被部分唯一索引拦截。文档 id 预生成，使旧版本
            # 的 superseded_by 在置失效时即可指向新文档（版本链不丢失）。
            document = Document(
                id=str(uuid.uuid4()),
                source_path=source_path, content_hash=content_hash,
                object_bucket=ref.bucket, object_key=ref.key, content_size=len(data),
                mime_type=mime_type, title=path.name,
                doc_type=extracted.doc_type, status="indexed",
            )
            superseded = self._supersede_other_versions(source_path, keep=document)
            self._db.flush()
            self._db.add(document)
            self._db.flush()
            count = self._replace_chunks(document, extracted.text)
            return {
                "ok": True, "document_id": document.id, "chunks": count,
                "content_hash": content_hash, "doc_type": extracted.doc_type,
                "mime_type": extracted.mime_type, "metadata": extracted.metadata,
                "superseded": superseded,
            }
        except Exception:
            logger.exception("知识文档摄取失败：%s", file_path)
            raise

    # ── 读取与重建 ────────────────────────────────────────────────

    def read_source(self, document_id: str) -> bytes:
        """从对象存储读取并校验指定文档的持久原文。"""
        document = self._db.get(Document, document_id)
        if document is None:
            raise LookupError("知识文档不存在")
        if not document.object_bucket or not document.object_key:
            raise LookupError("知识文档没有可用的持久原文")
        return get_verified(
            ObjectRef(bucket=document.object_bucket, key=document.object_key),
            document.content_hash,
        )

    def reindex(self, document_id: str, *, mark_failure: bool = True) -> dict[str, Any]:
        """仅从持久原文重新生成文档分块，不依赖 source_path。

        `mark_failure=False` 供维护任务使用：调用方在保存点回滚后自行写入失败状态，
        避免再开一个独立会话。
        """
        document = self._db.get(Document, document_id)
        if document is None:
            raise LookupError("知识文档不存在")
        if document.status in INACTIVE_STATUSES:
            raise ValueError("文档已失效，不能重新索引")
        try:
            data = self.read_source(document_id)
            extracted = extract_document_bytes(data, document.title or Path(document.source_path).name)
            count = self._replace_chunks(document, extracted.text)
            document.doc_type = extracted.doc_type
            document.mime_type = extracted.mime_type
            document.status = "indexed"
            document.updated_at = _now()
            self._db.flush()
            return {"ok": True, "document_id": document.id, "chunks": count}
        except Exception:
            logger.exception("知识文档重新索引失败：%s", document_id)
            # 在当前事务上赋值 status 后 raise 会被外层 rollback 抹掉（死代码）。
            # 用独立短会话落盘失败状态，保证 index_failed 可观测。
            if mark_failure:
                self._mark_index_failed(document_id)
            raise

    @staticmethod
    def _mark_index_failed(document_id: str) -> None:
        """用独立短会话写入 index_failed 状态（不受调用方事务回滚影响）。"""
        try:
            from aiive.db.base import SessionLocal

            side = SessionLocal()
            try:
                doc = side.get(Document, document_id)
                if doc is not None:
                    doc.status = "index_failed"
                    side.commit()
                else:
                    side.rollback()
            finally:
                side.close()
        except Exception:
            logger.warning("写入 index_failed 状态失败：%s", document_id, exc_info=True)

    # ── 删除 ──────────────────────────────────────────────────────

    def delete(self, document_id: str) -> dict[str, Any]:
        """软删除知识文档：清空分块、释放原文对象，保留墓碑记录。

        只清理 AIive 管理的派生数据（documents 行、chunks 行、对象存储原文），
        **不会删除 `source_path` 指向的用户原始文件**。
        """
        document = self._db.get(Document, document_id)
        if document is None:
            raise LookupError("知识文档不存在")
        if document.status == "deleted":
            return {
                "ok": True, "document_id": document_id, "already_deleted": True,
                "chunks_removed": 0, "object_released": False,
                "source_file_removed": False,
            }
        chunks_removed = self.purge_chunks(document.id)
        object_released = self.release_object(document)
        document.status = "deleted"
        document.deleted_at = _now()
        document.superseded_by = None
        self._db.flush()
        return {
            "ok": True, "document_id": document_id, "already_deleted": False,
            "chunks_removed": chunks_removed, "object_released": object_released,
            "source_path": document.source_path, "source_file_removed": False,
        }


def search_chunks(db: Session, query: str, limit: int = 5) -> list[dict[str, Any]]:
    """知识分块检索：词法 + 向量混合召回（向量不可用时自动退化为纯词法）。

    只返回活跃状态文档的分块：已取代（superseded）与已删除（deleted）的文档
    即使残留分块或向量也不会被召回。两路结果按 RRF 融合去重，词法命中优先级
    不低于向量命中，因此纯关键词查询的排序与接入向量前一致。
    """
    normalized = query.strip().casefold()
    if not normalized:
        return []
    limit = max(1, min(int(limit), 100))
    # 两路各取更深的候选再融合：融合层需要"另一路排名"才有意义，
    # 只取 limit 条会让 RRF 退化为单路排序的并集。深度有界，避免大库全扫。
    depth = min(limit * 4, 100)
    lexical_hits = _lexical_chunk_hits(db, normalized, depth)
    vector_hits = _vector_chunk_hits(db, query.strip(), depth)
    return _fuse_chunk_hits(lexical_hits, vector_hits, limit)


# 混合检索融合参数：RRF 让两路排名可比，权重表达"词法不弱于向量"的既有排序契约。
_HYBRID_RRF_K = 60
_HYBRID_WEIGHTS: dict[str, float] = {"lexical": 0.5, "vector": 0.5}


def _lexical_chunk_hits(db: Session, normalized: str, limit: int) -> list[dict[str, Any]]:
    """按边界化多关键词匹配与命中覆盖度排序（原有检索语义）。"""
    tokens = list(dict.fromkeys(token for token in re.split(r"\s+", normalized) if token))[:12]

    def escaped(token: str) -> str:
        return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    filters = [Chunk.content.ilike(f"%{escaped(token)}%", escape="\\") for token in tokens]
    candidates = (
        db.query(Chunk, Document)
        .join(Document, Chunk.document_id == Document.id)
        .filter(Document.status.notin_(tuple(INACTIVE_STATUSES)))
        .filter(or_(*filters))
        .order_by(Document.created_at.desc(), Chunk.chunk_index.asc())
        .limit(min(1000, limit * 20))
        .all()
    )
    results = sorted(
        candidates,
        key=lambda row: (
            normalized in row[0].content.casefold(),
            sum(token in row[0].content.casefold() for token in tokens),
            -row[0].chunk_index,
        ),
        reverse=True,
    )[:limit]
    return [_chunk_hit(chunk, document, [
        token for token in tokens if token in chunk.content.casefold()
    ]) for chunk, document in results]


def _vector_chunk_hits(db: Session, query: str, limit: int) -> list[dict[str, Any]]:
    """按语义相似度召回分块；任何失败都降级为空结果（保留关键词检索）。"""
    from aiive.config import settings
    from aiive.knowledge.chunk_vectors import ChunkVectorService, vector_table_ready

    try:
        if not vector_table_ready(db):
            return []
        rows = ChunkVectorService(db).search(
            query, min(limit, settings.aiive_knowledge_vector_top_k),
        )
    except Exception:
        logger.warning("知识向量召回失败，降级为关键词检索", exc_info=True)
        return []
    threshold = settings.aiive_knowledge_vector_min_similarity
    return [
        _chunk_hit(chunk, document, [], score=round(score, 6))
        for chunk, document, score in rows
        if score >= threshold
    ]


def _chunk_hit(
    chunk: Chunk,
    document: Document,
    matched_terms: list[str],
    *,
    score: float | None = None,
) -> dict[str, Any]:
    """构造统一的分块命中结构（词法/向量共用，附加字段不破坏既有契约）。"""
    hit: dict[str, Any] = {
        "chunk_id": chunk.id, "document_id": document.id,
        "source_path": document.source_path, "content_preview": chunk.content[:200],
        "line_start": chunk.line_start, "line_end": chunk.line_end,
        "matched_terms": matched_terms,
    }
    if score is not None:
        hit["vector_score"] = score
    return hit


def _fuse_chunk_hits(
    lexical_hits: list[dict[str, Any]],
    vector_hits: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """RRF 融合两路命中并按 chunk_id 去重；词法命中保留 matched_terms。"""
    scores: dict[str, float] = {}
    merged: dict[str, dict[str, Any]] = {}
    for route, hits in (("lexical", lexical_hits), ("vector", vector_hits)):
        for rank, hit in enumerate(hits, start=1):
            chunk_id = str(hit["chunk_id"])
            scores[chunk_id] = scores.get(chunk_id, 0.0) + _HYBRID_WEIGHTS[route] / (
                _HYBRID_RRF_K + rank
            )
            if chunk_id not in merged:
                merged[chunk_id] = dict(hit)
            elif route == "vector" and hit.get("vector_score") is not None:
                merged[chunk_id]["vector_score"] = hit["vector_score"]
    ranked = sorted(
        merged.values(),
        key=lambda hit: scores[str(hit["chunk_id"])],
        reverse=True,
    )[:limit]
    for hit in ranked:
        hit["hybrid_score"] = round(scores[str(hit["chunk_id"])], 6)
    return ranked
