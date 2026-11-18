"""Domain tool handlers (implementation) split from builtin_tools."""

import hashlib
import json
from typing import override

from aiive.tools.base import BaseTool, DatabaseTool
from aiive.context.run_context import RunContext
from aiive.knowledge.document_reader import ExtractedDocument
from aiive.runtime.attachment_cache import DocumentParseCache

from sqlalchemy.orm import Session

_LOCAL_DOCUMENTS = DocumentParseCache[ExtractedDocument](max_entry_bytes=10 * 1024 * 1024)


class ReadDocumentTool(BaseTool):
    @override
    def _run(self, file_path: str, max_chars: int = 4_000, offset: int = 0, version: str = ""):
        """从允许根目录提取结构化文档纯文本，不执行宏或嵌入内容。"""
        from aiive.knowledge.access import resolve_knowledge_path
        from aiive.knowledge.document_reader import MAX_DOCUMENT_BYTES, extract_document

        resolved = resolve_knowledge_path(file_path)
        def fingerprint() -> str:
            stat = resolved.stat()
            if stat.st_size > MAX_DOCUMENT_BYTES:
                raise ValueError("document_too_large")
            return hashlib.sha256(
                f"{resolved}:{stat.st_dev}:{stat.st_ino}:{stat.st_ctime_ns}:{stat.st_mtime_ns}:{stat.st_size}".encode()
            ).hexdigest()

        current = fingerprint()
        if offset and version != current:
            raise ValueError("document_version_changed_restart_at_zero")
        extracted = _LOCAL_DOCUMENTS.get((str(resolved), current), lambda: extract_document(resolved))
        if fingerprint() != current:
            raise ValueError("document_version_changed_restart_at_zero")
        limit = max(1, min(max_chars, 24_000))
        end = min(len(extracted.text), offset + limit)
        text = extracted.text[offset:end]
        # Count serialized content so heavily escaped pages also fit the Worker observation.
        while len(json.dumps(text, ensure_ascii=False)) > 29_000:
            text = text[:len(text) // 2]
        end = offset + len(text)
        return {
            "ok": True,
            "path": str(resolved),
            "doc_type": extracted.doc_type,
            "mime_type": extracted.mime_type,
            "text": text,
            "offset": offset,
            "version": current,
            "next_offset": end if end < len(extracted.text) else None,
            "truncated": end < len(extracted.text),
            "metadata": extracted.metadata,
        }


class IngestDocumentTool(DatabaseTool):
    @override
    def _run(self, db: Session, file_path: str, ctx: RunContext | None = None):
        """通过统一摄取服务持久化原文并导入知识库。"""
        from aiive.knowledge.access import resolve_knowledge_path
        from aiive.knowledge.ingestor import KnowledgeIngestor

        from aiive.forget.durable_sources import persist_sources
        from aiive.task_runtime.read_repository import inherit_sources

        result = KnowledgeIngestor(db).ingest(str(resolve_knowledge_path(file_path)))
        if ctx and result.get("ok") and not result.get("duplicate"):
            document_id = str(result["document_id"])
            if ctx.task_id:
                inherit_sources(db, "knowledge_document", document_id, [("agent_task", ctx.task_id)])
            elif ctx.turn_record_id:
                persist_sources(db, "knowledge_document", document_id, {"source_turn_record_id": ctx.turn_record_id})
        return result


class ReindexDocumentTool(DatabaseTool):
    @override
    def _run(self, db: Session, document_id: str):
        """从持久原文重新生成知识文档分块。"""
        from aiive.knowledge.ingestor import KnowledgeIngestor

        return KnowledgeIngestor(db).reindex(document_id)


class SearchKnowledgeTool(DatabaseTool):
    @override
    def _run(self, db: Session, query: str, limit: int = 5):
        """搜索已导入的文档知识。

        参数:
            query: 搜索查询
            limit: 返回结果数量上限，默认 5

        返回:
            search_chunks 的搜索结果
        """
        from aiive.knowledge.ingestor import search_chunks
        return search_chunks(db, query, limit)


class ReadKnowledgeChunkTool(DatabaseTool):
    @override
    def _run(self, db: Session, chunk_id: str, offset: int = 0, max_chars: int = 4000):
        from aiive.db.models import Chunk, Document
        from aiive.forget.durable_sources import references_visible
        from aiive.knowledge.access import document_sources, sources_payload
        from aiive.knowledge.document_status import ACTIVE_STATUSES

        row = db.query(Chunk, Document).join(Document, Document.id == Chunk.document_id).filter(
            Chunk.id == chunk_id, Document.status.in_(ACTIVE_STATUSES),
        ).first()
        if row is None:
            raise ValueError("knowledge_chunk_unavailable")
        chunk, document = row
        sources = document_sources(db, document.id)
        if not references_visible(db, sources):
            raise ValueError("knowledge_chunk_unavailable")
        end = min(len(chunk.content), offset + max_chars)
        return {"ok": True, "chunk_id": chunk.id, "document_id": document.id,
                "chunk_index": chunk.chunk_index, "heading_path": chunk.heading_path,
                "text": chunk.content[offset:end], "offset": offset,
                "next_offset": end if end < len(chunk.content) else None, **sources_payload(sources)}
