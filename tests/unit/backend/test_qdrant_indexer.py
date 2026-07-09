"""测试 QdrantIndexer（向量索引器）模块。

覆盖内存中 QdrantClient 的添加、搜索、删除集合及与 FakeEmbeddingClient 的集成索引。
"""

from aiive.knowledge.embedding_client import FakeEmbeddingClient
from aiive.knowledge.qdrant_indexer import QdrantClient, QdrantIndexer, QdrantPoint


class TestQdrantClient:
    """测试内存中 QdrantClient 的基础向量操作。"""

    def test_upsert_and_search(self):
        """添加向量后应能通过搜索找到最近邻。"""
        client = QdrantClient()
        client.create_collection("test", 3)
        client.upsert("test", [
            QdrantPoint(id="a", vector=[1.0, 0.0, 0.0]),
            QdrantPoint(id="b", vector=[0.0, 1.0, 0.0]),
        ])
        results = client.search("test", [1.0, 0.1, 0.0], limit=1)
        assert results[0].id == "a"

    def test_empty_collection(self):
        """空集合搜索应返回空列表。"""
        client = QdrantClient()
        client.create_collection("empty", 3)
        assert client.search("empty", [1.0, 0.0, 0.0]) == []

    def test_delete_collection(self):
        """删除集合后搜索应返回空列表。"""
        client = QdrantClient()
        client.create_collection("tmp", 3)
        client.delete_collection("tmp")
        assert client.search("tmp", [1.0, 0.0, 0.0]) == []


class TestQdrantIndexer:
    """测试 QdrantIndexer 的文本块索引和搜索功能。"""

    def test_index_and_search(self):
        """索引文本块后应能搜索到相关内容。"""
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
        """清理后应无搜索结果。"""
        indexer = QdrantIndexer(qdrant=QdrantClient(), embedding=FakeEmbeddingClient())
        indexer.ensure_collection()
        indexer.cleanup()
        assert indexer.search("test") == []
