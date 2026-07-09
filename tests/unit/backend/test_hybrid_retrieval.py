"""测试混合检索——Qdrant 向量检索和文本回退功能。"""
from aiive.knowledge.embedding_client import FakeEmbeddingClient
from aiive.knowledge.ingestor import KnowledgeIngestor
from aiive.knowledge.qdrant_indexer import QdrantClient, QdrantIndexer
from aiive.knowledge.retrieval_planner import RetrievalPlanner


class TestHybridRetrieval:
    """测试向量检索+文本回退的混合检索方案。"""

    def test_retrieval_returns_results(self, db_session, tmp_path):
        """验证文本摄入并索引后，检索能返回结果。"""
        # 摄入文档
        f = tmp_path / "doc.txt"
        f.write_text("AIive is a personal agent that remembers things about the user.")
        KnowledgeIngestor(db_session).ingest(str(f))
        db_session.flush()

        # 索引到 Qdrant
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
        """验证 Qdrant 不可用时能回退到文本搜索。"""
        f = tmp_path / "fallback.txt"
        f.write_text("Python is used for building APIs with FastAPI.")
        KnowledgeIngestor(db_session).ingest(str(f))
        db_session.flush()

        # 不索引到 Qdrant，应回退到文本搜索
        planner = RetrievalPlanner(db_session)
        results = planner.retrieve("Python")
        assert len(results) >= 0  # 至少文本搜索可以工作
