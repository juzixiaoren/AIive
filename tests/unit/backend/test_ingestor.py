from aiive.knowledge.chunker import chunk_text
from aiive.knowledge.ingestor import KnowledgeIngestor


class TestChunker:
    def test_chunk_short_text(self):
        chunks = chunk_text("hello world")
        assert len(chunks) == 1
        assert chunks[0]["content"] == "hello world"

    def test_chunk_long_text(self):
        text = "\n".join(f"Line {i}" for i in range(200))
        chunks = chunk_text(text, chunk_size=500)
        assert len(chunks) > 1

    def test_chunk_preserves_line_numbers(self):
        text = "a\nb\nc\nd\ne\nf\ng\nh\n"
        chunks = chunk_text(text, chunk_size=10)
        assert chunks[0]["line_start"] == 0


class TestIngestor:
    def test_ingest_creates_document_and_chunks(self, db_session, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# Hello\n\nThis is a test document.\n\n## Section\nContent here.")

        ingestor = KnowledgeIngestor(db_session)
        result = ingestor.ingest(str(f))
        db_session.flush()

        assert result["ok"] is True
        assert result["chunks"] > 0

    def test_duplicate_ingest_returns_existing(self, db_session, tmp_path):
        f = tmp_path / "dup.txt"
        f.write_text("unique content for dedup test")

        ingestor = KnowledgeIngestor(db_session)
        r1 = ingestor.ingest(str(f))
        db_session.flush()
        r2 = ingestor.ingest(str(f))
        db_session.flush()

        assert r1["ok"] is True
        assert r2["duplicate"] is True
        assert r2["document_id"] == r1["document_id"]
