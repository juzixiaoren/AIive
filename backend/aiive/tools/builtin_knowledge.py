"""Domain tool handlers (implementation) split from builtin_tools."""

from aiive.tools.base import BaseTool, DatabaseTool

from sqlalchemy.orm import Session



class ReadDocumentTool(BaseTool):
    def _run(self, file_path: str, max_chars: int = 50_000):
        """从允许根目录提取结构化文档纯文本，不执行宏或嵌入内容。"""
        from aiive.knowledge.access import resolve_knowledge_path
        from aiive.knowledge.document_reader import extract_document

        resolved = resolve_knowledge_path(file_path)
        extracted = extract_document(resolved)
        limit = max(1, min(max_chars, 200_000))
        return {
            "ok": True,
            "path": str(resolved),
            "doc_type": extracted.doc_type,
            "mime_type": extracted.mime_type,
            "text": extracted.text[:limit],
            "truncated": len(extracted.text) > limit,
            "metadata": extracted.metadata,
        }


class IngestDocumentTool(DatabaseTool):
    def _run(self, db: Session, file_path: str):
        """通过统一摄取服务持久化原文并导入知识库。"""
        from aiive.knowledge.access import resolve_knowledge_path
        from aiive.knowledge.ingestor import KnowledgeIngestor

        return KnowledgeIngestor(db).ingest(str(resolve_knowledge_path(file_path)))


class ReindexDocumentTool(DatabaseTool):
    def _run(self, db: Session, document_id: str):
        """从持久原文重新生成知识文档分块。"""
        from aiive.knowledge.ingestor import KnowledgeIngestor

        return KnowledgeIngestor(db).reindex(document_id)


class SearchKnowledgeTool(DatabaseTool):
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
