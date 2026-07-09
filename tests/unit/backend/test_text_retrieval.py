"""测试文本检索（Text Retrieval）功能。

验证 KnowledgeIngestor 的文档摄入和 search_chunks 的文本块检索。
"""

from aiive.knowledge.ingestor import KnowledgeIngestor, search_chunks
from aiive.db.models import Chunk, Document


class TestTextRetrieval:
    """测试文本块检索的基本功能。"""

    def test_search_finds_chunk(self, db_session, tmp_path):
        """搜索应找到包含关键词的文本块。"""
        f = tmp_path / "doc.txt"
        f.write_text("AIive is a personal agent. It remembers things about you.")
        KnowledgeIngestor(db_session).ingest(str(f))
        db_session.flush()

        results = search_chunks(db_session, "AIive", limit=5)
        assert len(results) >= 1
        assert "AIive" in results[0]["content_preview"]

    def test_search_no_match(self, db_session):
        """没有匹配时应返回空列表。"""
        results = search_chunks(db_session, "nonexistent_xyz")
        assert results == []

    def test_search_returns_chunk_metadata(self, db_session, tmp_path):
        """搜索结果应包含完整的元数据字段。"""
        f = tmp_path / "meta.txt"
        f.write_text("Python is great for building APIs.")
        KnowledgeIngestor(db_session).ingest(str(f))
        db_session.flush()

        results = search_chunks(db_session, "Python")
        assert len(results) >= 1
        assert "chunk_id" in results[0]
        assert "document_id" in results[0]
        assert "source_path" in results[0]
        assert "line_start" in results[0]
