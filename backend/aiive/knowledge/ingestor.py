"""
知识库文档摄取模块。

负责将文件读取、去重、分块后存入数据库，支持 markdown、代码和纯文本三种类型。
同时提供基于关键词的文本检索能力。
"""

import hashlib
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from aiive.db.models import Document, Chunk
from aiive.knowledge.chunker import chunk_text

logger = logging.getLogger(__name__)


class KnowledgeIngestor:
    """知识库文档摄取器，负责将文件导入知识库。"""

    def __init__(self, db: Session):
        """
        初始化摄取器。

        参数:
            db: SQLAlchemy 数据库会话。
        """
        self._db = db

    def ingest(self, file_path: str) -> dict:
        """
        摄取单个文件：读取、去重、分块、入库。

        参数:
            file_path: 待摄取的文件路径。

        返回:
            包含 ok、document_id、chunks 等字段的结果字典。
            如果文件已存在（基于内容哈希去重），返回 duplicate=True。
        """
        path = Path(file_path)
        if not path.exists():
            return {"ok": False, "error": "File not found", "path": file_path}

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            content_hash = hashlib.sha256(content.encode()).hexdigest()

            # 基于内容哈希去重
            existing = (
                self._db.query(Document)
                .filter(Document.content_hash == content_hash)
                .first()
            )
            if existing:
                chunks = (
                    self._db.query(Chunk)
                    .filter(Chunk.document_id == existing.id)
                    .order_by(Chunk.chunk_index)
                    .all()
                )
                return {
                    "ok": True, "duplicate": True, "document_id": existing.id,
                    "chunks": len(chunks),
                }

            # 根据文件后缀判断文档类型
            suffix = path.suffix.lower()
            if suffix in (".md", ".markdown"):
                doc_type = "markdown"
            elif suffix in (".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs"):
                doc_type = "code"
            else:
                doc_type = "text"

            doc = Document(
                source_path=str(path.resolve()),
                content_hash=content_hash,
                title=path.name,
                doc_type=doc_type,
            )
            self._db.add(doc)
            self._db.flush()

            # 文本分块并入库
            chunks_raw = chunk_text(content)
            chunk_ids = []
            for c in chunks_raw:
                chunk = Chunk(
                    document_id=doc.id,
                    content=c["content"],
                    line_start=c["line_start"],
                    line_end=c["line_end"],
                    chunk_index=c["chunk_index"],
                    token_estimate=max(1, len(c["content"]) // 4),  # 粗略 token 估算
                )
                self._db.add(chunk)
                chunk_ids.append(chunk.id)

            self._db.flush()
            return {"ok": True, "document_id": doc.id, "chunks": len(chunk_ids)}
        except Exception:
            logger.exception("知识库文件摄取失败: %s", file_path)
            raise


def search_chunks(db: Session, query: str, limit: int = 5) -> list[dict]:
    """
    基于关键词在数据库中进行全文检索（ILIKE）。

    参数:
        db: SQLAlchemy 数据库会话。
        query: 搜索关键词。
        limit: 返回结果数量上限，默认 5。

    返回:
        匹配的块信息列表，包含 chunk_id、document_id、source_path 等字段。
    """
    try:
        results = (
            db.query(Chunk, Document)
            .join(Document, Chunk.document_id == Document.id)
            .filter(Chunk.content.ilike(f"%{query}%"))
            .order_by(Chunk.chunk_index)
            .limit(limit)
            .all()
        )
        return [
            {
                "chunk_id": c.id,
                "document_id": d.id,
                "source_path": d.source_path,
                "content_preview": c.content[:200],  # 仅返回前200字符作为预览
                "line_start": c.line_start,
                "line_end": c.line_end,
            }
            for c, d in results
        ]
    except Exception:
        logger.exception("文本检索失败: query=%s", query)
        raise
