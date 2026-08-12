"""知识文档摄取、持久原文读取和重新索引模块。"""

import logging
import re
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy import or_

from aiive.db.models import Chunk, Document
from aiive.knowledge.chunker import chunk_text
from aiive.knowledge.document_reader import extract_document_bytes
from aiive.storage.object_store import ObjectRef, get_verified, put_content_addressed

logger = logging.getLogger(__name__)
KNOWLEDGE_BUCKET = "knowledge-documents"


class KnowledgeIngestor:
    """知识文档摄取器和持久原文访问入口。"""

    def __init__(self, db: Session):
        """绑定当前数据库事务。"""
        self._db: Session = db

    def _replace_chunks(self, document: Document, content: str) -> int:
        """在当前事务中用持久原文重建全部分块。"""
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
        return len(chunks)

    def ingest(self, file_path: str) -> dict[str, Any]:
        """持久化文件原文，并创建或复用文档及分块记录。"""
        path = Path(file_path)
        if not path.is_file():
            return {"ok": False, "error": "文件不存在", "path": file_path}
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
            if existing:
                if not existing.object_bucket or not existing.object_key:
                    existing.object_bucket = ref.bucket
                    existing.object_key = ref.key
                    existing.content_size = len(data)
                    existing.mime_type = mime_type
                    existing.status = "indexed"
                chunks = self._db.query(Chunk).filter(Chunk.document_id == existing.id).count()
                if chunks == 0:
                    # 去重命中但分块缺失（此前摄取失败/被清理）→ 补建分块
                    chunks = self._replace_chunks(
                        existing, extracted.text
                    )
                    existing.status = "indexed"
                self._db.flush()
                return {
                    "ok": True, "duplicate": True, "document_id": existing.id,
                    "chunks": chunks, "content_hash": content_hash,
                }

            document = Document(
                source_path=str(path.resolve()), content_hash=content_hash,
                object_bucket=ref.bucket, object_key=ref.key, content_size=len(data),
                mime_type=mime_type, title=path.name,
                doc_type=extracted.doc_type, status="indexed",
            )
            self._db.add(document)
            self._db.flush()
            count = self._replace_chunks(document, extracted.text)
            return {
                "ok": True, "document_id": document.id, "chunks": count,
                "content_hash": content_hash, "doc_type": extracted.doc_type,
                "mime_type": extracted.mime_type, "metadata": extracted.metadata,
            }
        except Exception:
            logger.exception("知识文档摄取失败：%s", file_path)
            raise

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

    def reindex(self, document_id: str) -> dict[str, Any]:
        """仅从持久原文重新生成文档分块，不依赖 source_path。"""
        document = self._db.get(Document, document_id)
        if document is None:
            raise LookupError("知识文档不存在")
        try:
            data = self.read_source(document_id)
            extracted = extract_document_bytes(data, document.title or Path(document.source_path).name)
            count = self._replace_chunks(document, extracted.text)
            document.doc_type = extracted.doc_type
            document.mime_type = extracted.mime_type
            document.status = "indexed"
            self._db.flush()
            return {"ok": True, "document_id": document.id, "chunks": count}
        except Exception:
            logger.exception("知识文档重新索引失败：%s", document_id)
            # 在当前事务上赋值 status 后 raise 会被外层 rollback 抹掉（死代码）。
            # 用独立短会话落盘失败状态，保证 index_failed 可观测。
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


def search_chunks(db: Session, query: str, limit: int = 5) -> list[dict[str, Any]]:
    """边界化多关键词检索，并按命中覆盖度排序。"""
    normalized = query.strip().casefold()
    if not normalized:
        return []
    limit = max(1, min(int(limit), 100))
    tokens = list(dict.fromkeys(token for token in re.split(r"\s+", normalized) if token))[:12]

    def escaped(token: str) -> str:
        return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    filters = [Chunk.content.ilike(f"%{escaped(token)}%", escape="\\") for token in tokens]
    candidates = (
        db.query(Chunk, Document)
        .join(Document, Chunk.document_id == Document.id)
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
    return [{
        "chunk_id": chunk.id, "document_id": document.id,
        "source_path": document.source_path, "content_preview": chunk.content[:200],
        "line_start": chunk.line_start, "line_end": chunk.line_end,
        "matched_terms": [token for token in tokens if token in chunk.content.casefold()],
    } for chunk, document in results]
