"""真实 pgvector + 本地 Embedding 服务的 RAG 链路测试（非 SQLite、非 Mock）。

前置条件（缺一即跳过，保证离线 CI 不受影响）：
1. `AIIVE_PG_TESTS=1`，且 `tests/integration/pg/conftest.py` 能提供真实 PostgreSQL + pgvector；
2. 本地 Embedding 服务（TEI，`AIIVE_EMBEDDING_BASE_URL`）健康可访问，模型为
   `BAAI/bge-small-zh-v1.5` 固定 revision（见 .env）。

覆盖：分块向量入库 → 语义召回 → 词法/向量融合 → 文档更新/删除/重建清理 →
模型服务故障降级 → 维护回填；以及长期记忆的 Outbox 投影 → 向量召回 →
统一检索进入上下文 → 归档/遗忘删除派生向量。
"""
from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from aiive.config import settings
from aiive.db.models import Chunk, Document, KnowledgeChunkVector, MemoryRecord
from aiive.knowledge.chunk_vectors import ChunkVectorService
from aiive.knowledge.ingestor import KnowledgeIngestor, search_chunks
from aiive.knowledge.maintenance import (
    ISSUE_MISSING_CHUNK_VECTORS,
    apply_report,
    scan_knowledge_base,
)
from aiive.memory.automatic_recall import AutomaticRecallEngine
from aiive.memory.recall_config import RecallConfig
from aiive.memory.recall_models import MemoryRecallRequest, ScopeContext
from aiive.memory.vector_projection import (
    embedding_model_identity,
)
from aiive.retrieval.retrieval_types import RetrievalMode, RetrievalRequest
from aiive.retrieval.unified_retriever import UnifiedRetriever
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome
from aiive.worker.outbox_handlers import handle_memory_vector_refresh
from sqlalchemy import func, text

EMBEDDING_BASE_URL = "http://127.0.0.1:8081/v1"


def _embedding_service_ready() -> bool:
    """探测本地 Embedding 服务是否可用（不可用则整模块跳过）。"""
    try:
        response = httpx.get(f"{EMBEDDING_BASE_URL}/health", timeout=2.0)
        return response.status_code < 500
    except Exception:  # noqa: BLE001 - 探测失败即视为不可用
        return False


pytestmark = pytest.mark.skipif(
    not _embedding_service_ready(),
    reason="本地 Embedding 服务不可用（启动 TEI: docker compose --profile local-embedding up -d embedding）",
)


@pytest.fixture(autouse=True)
def _real_embedding_settings(monkeypatch):
    """把向量能力指向真实本地模型；测试内的向量都由真实模型生成。"""
    monkeypatch.setattr(settings, "aiive_memory_vector_enabled", True)
    monkeypatch.setattr(settings, "aiive_knowledge_vector_enabled", True)
    monkeypatch.setattr(settings, "aiive_embedding_provider", "local")
    monkeypatch.setattr(settings, "aiive_embedding_base_url", EMBEDDING_BASE_URL)
    monkeypatch.setattr(settings, "aiive_embedding_api_key", "")
    monkeypatch.setattr(settings, "aiive_embedding_model", "BAAI/bge-small-zh-v1.5")
    monkeypatch.setattr(
        settings, "aiive_embedding_model_revision",
        "534b6bfaaf500e70bcb9f3771cebc940c23b219d",
    )
    monkeypatch.setattr(settings, "aiive_embedding_dimensions", 512)
    monkeypatch.setattr(
        settings, "aiive_embedding_query_prefix", "为这个句子生成表示以用于检索相关文章：",
    )
    monkeypatch.setattr(settings, "aiive_embedding_batch_size", 4)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _ingest(db, path: Path) -> dict:
    result = KnowledgeIngestor(db).ingest(str(path))
    db.flush()
    return result


def _vector_count(db) -> int:
    return int(db.query(func.count(KnowledgeChunkVector.chunk_id)).scalar() or 0)


def _chunk_ids(db, document_id: str) -> list[str]:
    return [
        str(row[0])
        for row in db.query(Chunk.id).filter(Chunk.document_id == document_id).all()
    ]


def _norm(vector) -> float:
    return math.sqrt(sum(float(value) ** 2 for value in vector))


# ── 1. 真实向量入库 ──────────────────────────────────────────────


def test_ingest_persists_real_vectors_for_every_chunk(pg_session, tmp_path):
    """摄取后每个分块都有真实模型生成的 512 维单位向量。"""
    db = pg_session
    path = _write(
        tmp_path, "deploy.md",
        "部署流程\n第一步构建镜像\n第二步推送到私有仓库\n第三步滚动重启服务\n"
        "回滚策略\n出现异常时切回上一版本镜像\n",
    )
    result = _ingest(db, path)
    db.commit()

    chunk_count = int(
        db.query(func.count(Chunk.id))
        .filter(Chunk.document_id == result["document_id"]).scalar() or 0
    )
    assert chunk_count > 0
    assert _vector_count(db) == chunk_count

    rows = db.query(KnowledgeChunkVector).all()
    assert {row.embedding_model for row in rows} == {embedding_model_identity()}
    for row in rows:
        assert len(row.embedding) == 512
        assert abs(_norm(row.embedding) - 1.0) < 1e-3
    # 维度在库侧同样是 512（不是应用层截断）
    dims = db.execute(text("SELECT DISTINCT vector_dims(embedding) FROM knowledge_chunk_vectors")).scalars().all()
    assert dims == [512]


# ── 2. 语义召回（无字面重叠）─────────────────────────────────────


def test_semantic_query_recalls_chunk_without_lexical_overlap(pg_session, tmp_path, monkeypatch):
    """查询词与目标分块无字面重叠时，仅向量通道能召回，混合结果包含它。"""
    db = pg_session
    target = _ingest(db, _write(
        tmp_path, "ops.md",
        "发布纪律\n上线前必须在预发环境完成冒烟验证，再由值班同学确认窗口。\n",
    ))
    _ingest(db, _write(
        tmp_path, "coffee.md",
        "饮食偏好\n喜欢美式咖啡，不加糖，下午三点后不喝。\n",
    ))
    db.commit()

    query = "发布前要做什么检查"
    # 纯词法：查询词与分块没有共同 token，应当为空
    monkeypatch.setattr(settings, "aiive_knowledge_vector_enabled", False)
    assert search_chunks(db, query, 5) == []
    monkeypatch.setattr(settings, "aiive_knowledge_vector_enabled", True)

    hybrid = search_chunks(db, query, 5)
    assert hybrid, "混合检索应至少返回向量命中的分块"
    assert hybrid[0]["document_id"] == target["document_id"]
    assert hybrid[0]["vector_score"] >= settings.aiive_knowledge_vector_min_similarity
    assert hybrid[0]["matched_terms"] == []


def test_hybrid_keeps_exact_term_ranking(pg_session, tmp_path):
    """精确术语查询仍以词法命中优先，向量命中不挤掉它。"""
    db = pg_session
    exact = _ingest(db, _write(
        tmp_path, "metrics.md",
        "监控指标\nrequest_latency_p95 是核心延迟指标，超过 800ms 触发告警。\n",
    ))
    _ingest(db, _write(
        tmp_path, "noise.md",
        "性能优化经验\n减少请求排队可以降低尾延迟，但指标名称不同。\n",
    ))
    db.commit()

    results = search_chunks(db, "request_latency_p95", 5)
    assert results
    assert results[0]["document_id"] == exact["document_id"]
    assert results[0]["matched_terms"] == ["request_latency_p95"]
    assert "hybrid_score" in results[0]


# ── 3. 生命周期：更新 / 删除 / 重建 清理旧向量 ──────────────────


def test_document_update_purges_superseded_vectors(pg_session, tmp_path):
    """同一路径内容更新后，旧版本分块与向量都不再可召回。"""
    db = pg_session
    path = _write(tmp_path, "policy.md", "旧版本\n报销需要纸质发票原件。\n")
    first = _ingest(db, path)
    db.commit()
    assert _vector_count(db) > 0

    path.write_text("新版本\n报销改为电子发票，无需纸质原件。\n", encoding="utf-8")
    second = _ingest(db, path)
    db.commit()

    assert second["document_id"] != first["document_id"]
    assert db.get(Document, first["document_id"]).status == "superseded"
    assert _chunk_ids(db, first["document_id"]) == []
    # 旧版本向量已随分块清理，只剩新版本
    assert _vector_count(db) == len(_chunk_ids(db, second["document_id"]))
    # 旧版本独有的字面词不再有任何词法命中（分块与向量都已清理）
    stale_hits = [hit for hit in search_chunks(db, "纸质发票", 5) if hit["matched_terms"]]
    assert stale_hits == []
    # 语义通道即使命中相近内容，也只会落在新版本上，绝不返回已取代文档
    assert {hit["document_id"] for hit in search_chunks(db, "纸质发票原件", 5)} <= {
        second["document_id"],
    }
    assert search_chunks(db, "电子发票", 5)[0]["document_id"] == second["document_id"]


def test_delete_purges_vectors_and_blocks_recall(pg_session, tmp_path):
    """删除文档后向量清空，且任何通道都不再召回。"""
    db = pg_session
    result = _ingest(db, _write(tmp_path, "temp.md", "临时资料\n季度预算复核记录。\n"))
    db.commit()
    assert _vector_count(db) > 0

    KnowledgeIngestor(db).delete(result["document_id"])
    db.commit()

    assert _vector_count(db) == 0
    assert search_chunks(db, "季度预算复核", 5) == []


def test_reindex_rebuilds_vectors_idempotently(pg_session, tmp_path):
    """重建分块后向量按当前分块补齐，且不产生重复投影。"""
    db = pg_session
    result = _ingest(db, _write(tmp_path, "guide.md", "操作指南\n重启前先备份数据库。\n"))
    db.commit()
    db.query(KnowledgeChunkVector).delete()
    db.commit()
    assert _vector_count(db) == 0

    KnowledgeIngestor(db).reindex(result["document_id"])
    db.commit()

    chunk_ids = _chunk_ids(db, result["document_id"])
    assert _vector_count(db) == len(chunk_ids)
    stored = {str(row[0]) for row in db.query(KnowledgeChunkVector.chunk_id).all()}
    assert stored == set(chunk_ids)


def test_vectors_from_other_model_are_never_used(pg_session, tmp_path, monkeypatch):
    """换了模型/版本的向量不参与检索（避免不同模型向量混用）。"""
    db = pg_session
    result = _ingest(db, _write(
        tmp_path, "model.md",
        "模型身份\n向量必须与生成它的模型版本绑定，换模型后旧向量不可复用。\n",
    ))
    db.commit()
    # 把投影伪装成另一个模型生成的向量
    db.query(KnowledgeChunkVector).update(
        {KnowledgeChunkVector.embedding_model: "other-model@deadbeef"},
    )
    db.commit()

    monkeypatch.setattr(settings, "aiive_knowledge_vector_enabled", False)
    lexical_only = search_chunks(db, "模型身份", 5)
    monkeypatch.setattr(settings, "aiive_knowledge_vector_enabled", True)
    hybrid = search_chunks(db, "模型身份", 5)

    assert [hit["document_id"] for hit in lexical_only] == [result["document_id"]]
    # 混合检索仍能靠词法命中，但没有任何向量分数（说明异模型向量被排除）
    assert [hit["document_id"] for hit in hybrid] == [result["document_id"]]
    assert "vector_score" not in hybrid[0]
    assert ChunkVectorService(db).search("模型身份", 5) == []


# ── 4. 故障降级 ──────────────────────────────────────────────────


def test_embedding_outage_degrades_to_lexical_without_corruption(
    pg_session, tmp_path, monkeypatch,
):
    """模型服务不可用时：摄取仍成功（无向量），检索退回关键词且不抛错。"""
    db = pg_session
    monkeypatch.setattr(settings, "aiive_embedding_base_url", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(settings, "aiive_embedding_timeout_seconds", 1)

    result = _ingest(db, _write(tmp_path, "fallback.md", "降级验证\n关键词检索仍然可用。\n"))
    db.commit()

    assert result["ok"] is True
    assert len(_chunk_ids(db, result["document_id"])) > 0
    assert _vector_count(db) == 0
    hits = search_chunks(db, "关键词检索", 5)
    assert [hit["document_id"] for hit in hits] == [result["document_id"]]
    assert "vector_score" not in hits[0]


# ── 5. 维护回填 ──────────────────────────────────────────────────


def test_maintenance_reports_missing_vectors_and_backfills(pg_session, tmp_path):
    """向量缺失能被扫描发现，并在维护动作中按批回填。"""
    db = pg_session
    result = _ingest(db, _write(tmp_path, "maint.md", "维护验证\n向量可以被重建。\n"))
    db.commit()
    db.query(KnowledgeChunkVector).delete()
    db.commit()

    report = scan_knowledge_base(db)
    missing = [item.count for item in report.issues if item.code == ISSUE_MISSING_CHUNK_VECTORS]
    assert missing == [1]

    actions = apply_report(db, report)
    db.commit()
    assert actions["chunk_vectors_backfilled"] == 1
    assert actions["chunk_vectors_backfill_failed"] == 0
    assert _vector_count(db) == len(_chunk_ids(db, result["document_id"]))
    after = [item.count for item in scan_knowledge_base(db).issues if item.code == ISSUE_MISSING_CHUNK_VECTORS]
    assert after == [0]


# ── 6. 长期记忆链路 ──────────────────────────────────────────────


def _new_memory(db, content: str, *, scope_type: str = "global", scope_id: str | None = None,
                lifecycle: str = "active", key: str | None = None) -> MemoryRecord:
    record = MemoryRecord(
        id=str(uuid.uuid4()),
        memory_type="preference",
        canonical_key=key or f"test.{uuid.uuid4().hex[:8]}",
        scope_type=scope_type,
        scope_id=scope_id,
        content=content,
        lifecycle_state=lifecycle,
        validity_state="valid",
        confidence=0.9,
        importance=0.7,
        retention_policy="normal",
        record_version=1,
        pinned=False,
        stability="stable",
        observed_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    db.add(record)
    db.flush()
    return record


def _refresh_projection(memory_id: str, version: int = 1) -> str:
    """走真实 Outbox handler 生成/删除投影（与生产同一条调用链）。"""
    job = ClaimedJob(
        id=str(uuid.uuid4()),
        job_type="memory_vector_refresh",
        payload={"memory_id": memory_id, "record_version": version},
        trace_id=memory_id,
        retry_count=0,
        max_retries=3,
        claim_token="test-claim",
        schema_version=1,
        worker_id="worker-test",
    )
    result = handle_memory_vector_refresh(job)
    assert result.outcome == HandlerOutcome.COMPLETED, result.reason
    return result.reason


def test_memory_vector_recall_reaches_agent_context(pg_session):
    """记忆写入 → Outbox 投影 → 向量召回 → 统一检索进入上下文。"""
    db = pg_session
    coffee = _new_memory(db, "用户偏好：每天上午喝一杯手冲咖啡，不加奶。")
    infra = _new_memory(db, "用户偏好：生产环境使用 PostgreSQL 16 与 pgvector 扩展。")
    db.commit()

    assert _refresh_projection(coffee.id) == "upserted"
    assert _refresh_projection(infra.id) == "upserted"
    db.commit()

    engine = AutomaticRecallEngine(db, RecallConfig())
    pack, traces = engine.recall(MemoryRecallRequest(
        query="早上喜欢喝什么饮品", scope_context=ScopeContext(),
    ))
    ids = [item.memory_id for item in pack.items]
    # 语义相关的那条排第一；无关记忆即使被低阈值放行也不能排在前面
    assert ids[0] == coffee.id
    vector_traces = [t for t in traces if t.route == "vector"]
    assert any(t.memory_id == coffee.id and t.selected for t in vector_traces)
    assert infra.id != ids[0]

    # 与 ContextAssembler 完全相同的入口：统一检索 → 记忆命中 → 进入上下文分区
    result = UnifiedRetriever(db, None, RecallConfig()).retrieve(RetrievalRequest(
        query="早上喜欢喝什么饮品",
        mode=RetrievalMode.AUTO,
        scope_context=ScopeContext(),
        thread_id=None,
    ))
    memory_hits = [h for h in result.hits if h.source_type == "memory_record"]
    assert any(h.source_id == coffee.id for h in memory_hits)
    assert any("咖啡" in (h.snippet or "") for h in memory_hits)


def test_memory_archive_and_forget_remove_derived_vectors(pg_session):
    """归档 / 遗忘后派生向量被删除，且不再被任何召回路径返回。"""
    db = pg_session
    record = _new_memory(db, "用户偏好：周末徒步时带足饮水。")
    db.commit()
    assert _refresh_projection(record.id) == "upserted"
    db.commit()

    record.lifecycle_state = "archived"
    record.record_version = 2
    db.flush()
    db.commit()
    assert _refresh_projection(record.id, 2) == "inactive_deleted"
    db.commit()

    engine = AutomaticRecallEngine(db, RecallConfig())
    pack, _ = engine.recall(MemoryRecallRequest(
        query="周末徒步带什么", scope_context=ScopeContext(),
    ), include_archived=False)
    assert record.id not in [item.memory_id for item in pack.items]

    record.lifecycle_state = "forgotten"
    record.record_version = 3
    db.flush()
    db.commit()
    assert _refresh_projection(record.id, 3) == "already_absent"


def test_memory_backfill_bootstraps_only_missing_projections(pg_session):
    """历史记忆回填：只入队缺失/过期的 active+valid 记录，且可幂等重跑。"""
    db = pg_session
    first = _new_memory(db, "用户偏好：常用编辑器是 VSCode。")
    second = _new_memory(db, "用户偏好：终端使用 zsh + tmux。")
    archived = _new_memory(db, "用户偏好：旧笔记本已退役。", lifecycle="archived")
    db.commit()
    assert _refresh_projection(second.id) == "upserted"
    db.commit()

    from aiive.db.models import OutboxJob
    from aiive.memory.vector_bootstrap import ensure_memory_vector_backfill

    assert ensure_memory_vector_backfill() == 1
    jobs = db.query(OutboxJob).filter(OutboxJob.job_type == "memory_vector_refresh").all()
    pending = [job for job in jobs if job.payload.get("memory_id") == first.id]
    assert len(pending) == 1
    assert all(job.payload.get("memory_id") != archived.id for job in jobs)
    # 幂等：同一 generation 不会重复入队
    assert ensure_memory_vector_backfill() == 0

    job = pending[0]
    result = handle_memory_vector_refresh(ClaimedJob(
        id=job.id, job_type="memory_vector_refresh", payload=job.payload,
        trace_id=job.trace_id, retry_count=0, max_retries=3,
        claim_token="test-claim", schema_version=1, worker_id="worker-test",
    ))
    assert result.outcome == HandlerOutcome.COMPLETED
    db.expire_all()
    assert db.query(KnowledgeChunkVector).count() == 0
    assert _memory_projection_count(db) == 2


def _memory_projection_count(db) -> int:
    from aiive.db.models import MemoryVectorProjection

    return int(db.query(MemoryVectorProjection).count())


def test_memory_vector_query_respects_scope(pg_session):
    """向量召回沿用 scope 约束：其他 thread 的记忆不会跨界返回。"""
    db = pg_session
    other = _new_memory(
        db, "用户偏好：这个线程里用英文回复。",
        scope_type="thread", scope_id=str(uuid.uuid4()),
    )
    db.commit()
    assert _refresh_projection(other.id) == "upserted"
    db.commit()

    engine = AutomaticRecallEngine(db, RecallConfig())
    pack, _ = engine.recall(MemoryRecallRequest(
        query="用英文回复", scope_context=ScopeContext(thread_id=str(uuid.uuid4())),
    ))
    assert other.id not in [item.memory_id for item in pack.items]
