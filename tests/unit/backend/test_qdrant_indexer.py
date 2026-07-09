from aiive.knowledge.embedding_client import FakeEmbeddingClient
from aiive.knowledge.qdrant_indexer import QdrantClient, QdrantIndexer, QdrantPoint


class TestQdrantClient:
    def test_upsert_and_search(self):
        client = QdrantClient()
        client.create_collection("test", 3)
        client.upsert("test", [
            QdrantPoint(id="a", vector=[1.0, 0.0, 0.0]),
            QdrantPoint(id="b", vector=[0.0, 1.0, 0.0]),
        ])
        results = client.search("test", [1.0, 0.1, 0.0], limit=1)
        assert results[0].id == "a"

    def test_empty_collection(self):
        client = QdrantClient()
        client.create_collection("empty", 3)
        assert client.search("empty", [1.0, 0.0, 0.0]) == []

    def test_delete_collection(self):
        client = QdrantClient()
        client.create_collection("tmp", 3)
        client.delete_collection("tmp")
        assert client.search("tmp", [1.0, 0.0, 0.0]) == []


class TestQdrantIndexer:
    def test_index_and_search(self):
        fake_emb = FakeEmbeddingClient()
        qdrant = QdrantClient()
        indexer = QdrantIndexer(qdrant=qdrant, embedding=fake_emb)
        indexer.ensure_collection()

        chunks = [
            {"chunk_id": "c1", "document_id": "d1", "content": "AIive is a personal agent"},
            {"chunk_id": "c2", "document_id": "d1", "content": "Python and FastAPI backend"},
        ]
        indexer.index_chunks(chunks)
        results = indexer.search("personal agent", limit=1)
        assert len(results) > 0
        assert "chunk_id" in results[0].payload

    def test_cleanup(self):
        indexer = QdrantIndexer(qdrant=QdrantClient(), embedding=FakeEmbeddingClient())
        indexer.ensure_collection()
        indexer.cleanup()
        assert indexer.search("test") == []
