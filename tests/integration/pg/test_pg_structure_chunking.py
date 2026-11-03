"""真实 PostgreSQL + pgvector 的结构感知切分契约测试。

前置条件：AIIVE_PG_TESTS=1（由 tests/integration/pg/conftest.py 提供真实库）。
不依赖本地 Embedding 服务：向量由确定性假实现生成，只验证链路与元数据契约。

覆盖：
- 结构元数据（heading_path / section_id / block_type / 版本 / Embedding 哈希）落库；
- 向量投影记录输入模板版本与完整输入哈希，重复刷新幂等；
- 解析/切分版本落后时维护扫描发现并从持久原文重建（原文未变也要重建）；
- 投影哈希/模板版本被改坏时按批回填，且不产生新旧混合；
- 检索命中带结构定位，相邻 Chunk 扩展遵守 token 预算且不跨 Section。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from aiive.config import settings
from aiive.db.models import Chunk, Document, KnowledgeChunkVector
from aiive.knowledge import chunk_vectors
from aiive.knowledge.chunk_vectors import ChunkVectorService, missing_vector_document_ids
from aiive.knowledge.chunker import CHUNKER_VERSION
from aiive.knowledge.embedding_input import EMBEDDING_INPUT_VERSION
from aiive.knowledge.ingestor import KnowledgeIngestor, search_chunks
from aiive.knowledge.maintenance import (
    ISSUE_MISSING_CHUNK_VECTORS,
    ISSUE_STALE_CHUNKING,
    apply_report,
    scan_knowledge_base,
)
from aiive.memory.vector_projection import embedding_model_identity

MARKDOWN = (
    "# 部署手册\n"
    "\n"
    "## 发布流程\n"
    "\n"
    "第一步构建镜像，第二步推送到私有仓库，第三步滚动重启服务。\n"
    "\n"
    "- 先灰度一批\n"
    "- 再全量发布\n"
    "\n"
    "## 回滚\n"
    "\n"
    "出现异常时切回上一版本镜像。\n"
)


class _FakeEmbedder:
    """确定性假 Embedding：记录输入，返回固定 512 维向量。"""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def embed_batch(
        self, texts: list[str], *, input_type: str = "document", batch_size: int = 8,
    ) -> list[list[float]]:
        self.batches.append(list(texts))
        return [[0.02] * 512 for _ in texts]

    def embed(self, text: str, *, input_type: str = "query") -> list[float]:
        return [0.02] * 512


@pytest.fixture(autouse=True)
def _vector_runtime(monkeypatch):
    """打开知识向量通道，并使用固定的模型身份。"""
    # 默认关闭向量通道：摄取阶段不访问真实 Embedding 服务；
    # 需要验证投影的用例在摄取完成后单独打开。
    monkeypatch.setattr(settings, "aiive_knowledge_vector_enabled", False)
    monkeypatch.setattr(settings, "aiive_memory_vector_enabled", True)
    monkeypatch.setattr(settings, "aiive_embedding_model", "fake-embedding")
    monkeypatch.setattr(settings, "aiive_embedding_model_revision", "rev1")
    monkeypatch.setattr(settings, "aiive_knowledge_neighbor_expansion", 1)
    monkeypatch.setattr(settings, "aiive_knowledge_neighbor_budget_tokens", 600)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _ingest(db, path: Path) -> dict:
    result = KnowledgeIngestor(db).ingest(str(path))
    db.flush()
    return result


def _chunks(db, document_id: str) -> list[Chunk]:
    return (
        db.query(Chunk)
        .filter(Chunk.document_id == document_id)
        .order_by(Chunk.chunk_index.asc())
        .all()
    )


def _vectors(db, document_id: str) -> list[KnowledgeChunkVector]:
    return (
        db.query(KnowledgeChunkVector)
        .join(Chunk, Chunk.id == KnowledgeChunkVector.chunk_id)
        .filter(Chunk.document_id == document_id)
        .all()
    )


def test_ingest_persists_structure_and_refreshes_vectors(pg_session, tmp_path, monkeypatch):
    """真实 PG 上分块结构元数据与向量投影同时落库，且哈希覆盖 Embedding 输入。"""
    db = pg_session
    result = _ingest(db, _write(tmp_path, "deploy.md", MARKDOWN))
    document = db.get(Document, result["document_id"])
    chunks = _chunks(db, result["document_id"])

    assert document is not None
    assert chunks
    assert {chunk.heading_path for chunk in chunks} >= {"部署手册 > 发布流程", "部署手册 > 回滚"}
    assert all(chunk.section_id.startswith("s") for chunk in chunks)
    assert all(chunk.embedding_input_hash for chunk in chunks)
    assert all(chunk.source_type == document.doc_type for chunk in chunks)

    monkeypatch.setattr(settings, "aiive_knowledge_vector_enabled", True)
    fake = _FakeEmbedder()
    ChunkVectorService(db, provider_factory=lambda: fake).refresh_document(result["document_id"])
    db.flush()

    identity = embedding_model_identity()
    vectors = _vectors(db, result["document_id"])
    assert len(vectors) == len(chunks)
    assert all(row.input_version == EMBEDDING_INPUT_VERSION for row in vectors)
    assert all(row.embedding_model == identity for row in vectors)
    assert {row.content_hash for row in vectors} == {
        chunk.embedding_input_hash for chunk in chunks
    }
    assert fake.batches and all(
        text.startswith("文档：deploy.md\n章节：") for text in fake.batches[0]
    )
    assert missing_vector_document_ids(db, 10) == []

    # 幂等：内容与版本未变时不重复调用模型
    ChunkVectorService(db, provider_factory=lambda: fake).refresh_document(result["document_id"])
    db.flush()
    assert len(fake.batches) == 1


def test_stale_chunking_is_rebuilt_even_when_source_is_unchanged(pg_session, tmp_path):
    """切分器版本落后时，原文不变也要重新解析、切分并刷新向量。"""
    db = pg_session
    result = _ingest(db, _write(tmp_path, "deploy.md", MARKDOWN))
    document = db.get(Document, result["document_id"])
    assert document is not None
    document.chunker_version = "structure_recursive_v0"
    db.flush()
    before = {chunk.id for chunk in _chunks(db, result["document_id"])}

    report = scan_knowledge_base(db)
    stale = [item.count for item in report.issues if item.code == ISSUE_STALE_CHUNKING]
    assert stale == [1]

    actions = apply_report(db, report)
    db.flush()
    after = _chunks(db, result["document_id"])

    assert actions["documents_rebuilt"] == 1
    assert document.chunker_version == CHUNKER_VERSION
    assert before and after and before.isdisjoint({chunk.id for chunk in after})
    assert all(chunk.chunker_version == CHUNKER_VERSION for chunk in after)
    assert all(chunk.embedding_input_hash for chunk in after)


def test_corrupted_projection_is_detected_and_backfilled(pg_session, tmp_path, monkeypatch):
    """投影哈希或模板版本被改坏后，扫描能发现并按批回填，不留下新旧混合。"""
    db = pg_session
    result = _ingest(db, _write(tmp_path, "deploy.md", MARKDOWN))
    fake = _FakeEmbedder()
    # 维护回填内部自行构造 ChunkVectorService，这里替换默认 provider 工厂
    monkeypatch.setattr(chunk_vectors, "build_embedding_provider", lambda: fake)
    service = ChunkVectorService(db, provider_factory=lambda: fake)
    service.refresh_document(result["document_id"])
    db.flush()

    rows = _vectors(db, result["document_id"])
    assert rows
    rows[0].content_hash = "0" * 64
    rows[0].input_version = ""
    db.flush()

    monkeypatch.setattr(settings, "aiive_knowledge_vector_enabled", True)
    assert missing_vector_document_ids(db, 10) == [result["document_id"]]
    # 版本不一致的投影不参与向量召回（不与新模板向量混合排序）
    recalled = ChunkVectorService(db, provider_factory=lambda: fake).search("发布流程", 10)
    assert rows[0].chunk_id not in {chunk.id for chunk, _document, _score in recalled}
    report = scan_knowledge_base(db)
    missing = [item.count for item in report.issues if item.code == ISSUE_MISSING_CHUNK_VECTORS]
    assert missing == [1]

    actions = apply_report(db, report)
    db.flush()

    assert actions["chunk_vectors_backfilled"] == 1
    assert missing_vector_document_ids(db, 10) == []
    refreshed = {row.chunk_id: row for row in _vectors(db, result["document_id"])}
    for chunk in _chunks(db, result["document_id"]):
        row = refreshed[chunk.id]
        assert row.input_version == EMBEDDING_INPUT_VERSION
        assert row.content_hash == chunk.embedding_input_hash


def test_search_returns_structure_and_bounded_neighbors(pg_session, tmp_path, monkeypatch):
    """命中带标题路径与来源定位；邻居扩展同 Section、去重且不超 token 预算。"""
    db = pg_session
    paragraph = "".join(f"第{i}步说明发布流程的第{i}个细节。" for i in range(1, 121))
    text = "# 主题\n\n## 发布流程\n\n" + paragraph + "\n\n## 回滚\n\n切回上一版本镜像。\n"
    result = _ingest(db, _write(tmp_path, "flow.md", text))

    hits = search_chunks(db, "第60步", 2)
    assert hits
    seed = hits[0]
    assert seed["document_id"] == result["document_id"]
    assert seed["heading_path"] == "主题 > 发布流程"
    assert seed["block_type"] in {"paragraph", "mixed"}

    neighbors = [hit for hit in hits[1:] if hit.get("expanded_from")]
    assert neighbors
    assert all(hit["section_id"] == seed["section_id"] for hit in neighbors)
    assert len({hit["chunk_id"] for hit in hits}) == len(hits)
    assert sum(int(hit["token_estimate"]) for hit in neighbors) <= 600
