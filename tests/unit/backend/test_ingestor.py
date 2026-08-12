"""测试知识摄入器——文档分块、持久原文、摄入和去重功能。"""
from aiive.db.models import Chunk, Document
from aiive.knowledge.chunker import chunk_text
from aiive.knowledge.ingestor import KnowledgeIngestor
from aiive.knowledge.ingestor import search_chunks
from aiive.storage.object_store import ObjectRef, exists


class TestChunker:
    """测试文本分块器的各项行为。"""

    def test_chunk_short_text(self):
        """验证短文本只产生一个分块。"""
        chunks = chunk_text("hello world")
        assert len(chunks) == 1
        assert chunks[0]["content"] == "hello world"

    def test_chunk_long_text(self):
        """验证长文本被正确分割为多个分块。"""
        text = "\n".join(f"Line {i}" for i in range(200))
        chunks = chunk_text(text, chunk_size=500)
        assert len(chunks) > 1

    def test_chunk_preserves_line_numbers(self):
        """验证分块保留行号信息。"""
        text = "a\nb\nc\nd\ne\nf\ng\nh\n"
        chunks = chunk_text(text, chunk_size=10)
        assert chunks[0]["line_start"] == 0


class TestIngestor:
    """测试 KnowledgeIngestor 的文档摄入功能。"""

    def test_ingest_creates_document_and_chunks(self, db_session, tmp_path):
        """验证摄入操作创建文档和分块记录。"""
        f = tmp_path / "test.md"
        f.write_text("# Hello\n\nThis is a test document.\n\n## Section\nContent here.")

        ingestor = KnowledgeIngestor(db_session)
        result = ingestor.ingest(str(f))
        db_session.flush()

        assert result["ok"] is True
        assert result["chunks"] > 0

    def test_duplicate_ingest_returns_existing(self, db_session, tmp_path):
        """验证重复摄入时返回已有文档信息。"""
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

    def test_ingest_persists_content_addressed_source(self, db_session, tmp_path):
        """验证摄取结果保存内容寻址对象引用及完整元数据。"""
        source = tmp_path / "durable.md"
        source.write_bytes("# 持久知识\n原文内容".encode("utf-8"))

        result = KnowledgeIngestor(db_session).ingest(str(source))
        document = db_session.get(Document, result["document_id"])

        assert document is not None
        assert document.object_bucket == "knowledge-documents"
        assert document.object_key.endswith(document.content_hash)
        assert document.content_size == len(source.read_bytes())
        assert document.mime_type == "text/markdown"
        assert document.status == "indexed"
        assert exists(ObjectRef(document.object_bucket, document.object_key))

    def test_read_and_reindex_survive_source_removal(self, db_session, tmp_path):
        """验证来源文件删除后仍能读取持久原文并恢复分块。"""
        source = tmp_path / "movable.txt"
        original = "对象存储是知识原文的长期真相源。"
        source.write_text(original, encoding="utf-8")
        ingestor = KnowledgeIngestor(db_session)
        result = ingestor.ingest(str(source))
        document_id = result["document_id"]

        source.rename(tmp_path / "moved.txt")
        db_session.query(Chunk).filter(Chunk.document_id == document_id).delete()
        db_session.flush()

        assert ingestor.read_source(document_id).decode("utf-8") == original
        rebuilt = ingestor.reindex(document_id)
        chunks = db_session.query(Chunk).filter(Chunk.document_id == document_id).all()
        assert rebuilt["chunks"] == 1
        assert [chunk.content for chunk in chunks] == [original]

    def test_same_content_uses_same_object_key(self, db_session, tmp_path):
        """验证不同来源的相同原文幂等复用同一内容地址。"""
        first = tmp_path / "first.txt"
        second = tmp_path / "second.txt"
        first.write_text("相同原文", encoding="utf-8")
        second.write_text("相同原文", encoding="utf-8")
        ingestor = KnowledgeIngestor(db_session)

        one = ingestor.ingest(str(first))
        two = ingestor.ingest(str(second))
        document = db_session.get(Document, one["document_id"])

        assert two["duplicate"] is True
        assert two["document_id"] == one["document_id"]
        assert document.object_key.endswith(one["content_hash"])

    def test_search_is_multi_term_ranked_and_escapes_wildcards(self, db_session, tmp_path):
        first = tmp_path / "first.md"
        second = tmp_path / "second.md"
        first.write_text("季度报告\n收入增长 20%\n产品表现稳定", encoding="utf-8")
        second.write_text("收入说明\n没有增长数据", encoding="utf-8")
        ingestor = KnowledgeIngestor(db_session)
        first_result = ingestor.ingest(str(first))
        ingestor.ingest(str(second))

        results = search_chunks(db_session, "收入 增长 20%", 5)
        assert results[0]["document_id"] == first_result["document_id"]
        assert results[0]["matched_terms"] == ["收入", "增长", "20%"]
        assert [item["document_id"] for item in search_chunks(db_session, "%", 5)] == [
            first_result["document_id"],
        ]
        assert search_chunks(db_session, "_missing_", 5) == []
