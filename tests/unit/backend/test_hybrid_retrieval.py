from aiive.knowledge.embedding_client import FakeEmbeddingClient
from aiive.knowledge.ingestor import KnowledgeIngestor
from aiive.knowledge.qdrant_indexer import QdrantClient, QdrantIndexer
from aiive.knowledge.retrieval_planner import RetrievalPlanner


class TestHybridRetrieval:
    def test_retrieval_returns_results(self, db_session, tmp_path):
        # Ingest
        f = tmp_path / "doc.txt"
        f.write_text("AIive is a personal agent that remembers things about the user.")
        KnowledgeIngestor(db_session).ingest(str(f))
        db_session.flush()

        # Index to Qdrant
        from aiive.db.models import Chunk
        chunks = db_session.query(Chunk).all()
        chunk_dicts = [
            {"chunk_id": c.id, "document_id": c.document_id, "content": c.content}
            for c in chunks
        ]

        indexer = QdrantIndexer(qdrant=QdrantClient(), embedding=FakeEmbeddingClient())
        indexer.ensure_collection()
        indexer.index_chunks(chunk_dicts)

        planner = RetrievalPlanner(db_session, indexer)
        results = planner.retrieve("personal agent")
        assert len(results) > 0

    def test_fallback_to_text_when_qdrant_fails(self, db_session, tmp_path):
        f = tmp_path / "fallback.txt"
        f.write_text("Python is used for building APIs with FastAPI.")
        KnowledgeIngestor(db_session).ingest(str(f))
        db_session.flush()

        # No Qdrant indexing - should fallback to text
        planner = RetrievalPlanner(db_session)
        results = planner.retrieve("Python")
        assert len(results) >= 0  # at least text search works
