from aiive.knowledge.ingestor import KnowledgeIngestor, search_chunks
from aiive.db.models import Chunk, Document


class TestTextRetrieval:
    def test_search_finds_chunk(self, db_session, tmp_path):
        f = tmp_path / "doc.txt"
        f.write_text("AIive is a personal agent. It remembers things about you.")
        KnowledgeIngestor(db_session).ingest(str(f))
        db_session.flush()

        results = search_chunks(db_session, "AIive", limit=5)
        assert len(results) >= 1
        assert "AIive" in results[0]["content_preview"]

    def test_search_no_match(self, db_session):
        results = search_chunks(db_session, "nonexistent_xyz")
        assert results == []

    def test_search_returns_chunk_metadata(self, db_session, tmp_path):
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
