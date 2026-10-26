"""知识库检索效果评测：纯关键词 / 纯向量 / 混合检索对比。

前置条件（真实模型 + 真实 PostgreSQL/pgvector，不使用 Mock）：
1. 本地 TEI 已启动：`docker compose --profile local-embedding up -d postgres embedding`
2. .env 中已配置 AIIVE_EMBEDDING_*（模型、revision、query 前缀、512 维）
3. 可用数据库：默认自动创建 `aiive_rag_eval`（可用 AIIVE_RAG_EVAL_DB_URL 覆盖）

用法：
    cd <repo> && .venv/Scripts/python.exe scripts/rag_eval/run_eval.py

同一份语料、同一过滤条件、同一 Top-K 下比较三种检索模式，输出
Recall@5、MRR@5、平均/P95 延迟与典型查询的 Top-5 结果。
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_ROOT = _REPO_ROOT / "backend"
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

DATA_DIR = Path(__file__).resolve().parent
EVAL_DB_URL = os.environ.get(
    "AIIVE_RAG_EVAL_DB_URL",
    "postgresql+psycopg://aiive:aiive_dev@127.0.0.1:5432/aiive_rag_eval",
)
MODES = ("lexical", "vector", "hybrid")


def _ensure_database() -> None:
    """确保评测库存在（连接到同实例的 maintenance 库创建）。"""
    from sqlalchemy.engine import make_url

    url = make_url(EVAL_DB_URL)
    admin_url = url.set(database="postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": url.database},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        admin.dispose()


def _prepare_schema(engine) -> None:
    """建扩展与全部表（评测库与生产迁移后的结构一致）。"""
    from aiive.db.models import Base

    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)


def _seed_corpus(db: Session, corpus: dict[str, Any], workdir: Path) -> dict[str, str]:
    """按语料状态真实导入，返回 document_id -> slug 的标注映射。

    - active：正常导入；
    - superseded：先导入旧内容，再用同路径新内容取代（旧文档不再有分块/向量）；
    - deleted：导入后立即删除（软删除，分块与向量清空）。
    """
    from aiive.db.models import Chunk, Document, KnowledgeChunkVector
    from aiive.knowledge.ingestor import KnowledgeIngestor

    db.query(KnowledgeChunkVector).delete()
    db.query(Chunk).delete()
    db.query(Document).delete()
    db.commit()

    ingestor = KnowledgeIngestor(db)
    document_slug: dict[str, str] = {}
    for doc in corpus["docs"]:
        slug = doc["slug"]
        path = workdir / f"{slug}.md"
        path.write_text(doc["content"], encoding="utf-8")
        status = doc["status"]
        if status == "superseded":
            # 先导入旧内容，再用同路径新内容取代，验证旧版本不再被召回
            path.write_text(doc["content"] + "\n\n（本版本已过期）", encoding="utf-8")
            old = ingestor.ingest(str(path))
            db.flush()
            path.write_text(doc["content"] + "\n\n（已由新政策取代，不再生效）", encoding="utf-8")
            ingestor.ingest(str(path))
            db.flush()
            document_slug[old["document_id"]] = slug
            continue
        result = ingestor.ingest(str(path))
        db.flush()
        document_slug[result["document_id"]] = slug
        if status == "deleted":
            ingestor.delete(result["document_id"])
            db.flush()
    db.commit()
    return document_slug


def _run_lexical(db: Session, query: str, top_k: int) -> list[str]:
    """纯关键词检索：关闭向量通道后调用同一入口。"""
    from aiive.config import settings
    from aiive.knowledge.ingestor import search_chunks

    original = settings.aiive_knowledge_vector_enabled
    settings.aiive_knowledge_vector_enabled = False
    try:
        hits = search_chunks(db, query, top_k)
    finally:
        settings.aiive_knowledge_vector_enabled = original
    return [hit["document_id"] for hit in hits]


def _run_vector(db: Session, query: str, top_k: int) -> list[str]:
    """纯向量检索：只用余弦近邻 + 与混合模式相同的相似度下限。"""
    from aiive.config import settings
    from aiive.knowledge.chunk_vectors import ChunkVectorService

    # 与混合检索相同的候选深度，再按相似度下限过滤并截断到 Top-K
    rows = ChunkVectorService(db).search(query, min(top_k * 4, 100))
    return [
        chunk.document_id
        for chunk, _document, score in rows
        if score >= settings.aiive_knowledge_vector_min_similarity
    ][:top_k]


def _run_hybrid(db: Session, query: str, top_k: int) -> list[str]:
    """混合检索：关键词 + 向量 RRF 融合（生产路径）。"""
    from aiive.knowledge.ingestor import search_chunks

    return [hit["document_id"] for hit in search_chunks(db, query, top_k)]


_RUNNERS = {"lexical": _run_lexical, "vector": _run_vector, "hybrid": _run_hybrid}


def _evaluate(
    db: Session, queries: list[dict[str, Any]], slug_map: dict[str, str], top_k: int,
) -> dict[str, Any]:
    """逐模式执行全部查询并计算指标。"""
    results: dict[str, Any] = {}
    for mode in MODES:
        runner = _RUNNERS[mode]
        records: list[dict[str, Any]] = []
        latencies: list[float] = []
        for item in queries:
            started = time.perf_counter()
            raw_ids = runner(db, item["query"], top_k)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            latencies.append(elapsed_ms)
            slugs = [slug_map.get(str(doc_id), str(doc_id)) for doc_id in raw_ids]
            expected = set(item.get("expected") or [])
            forbidden = set(item.get("forbidden") or [])
            hit_slugs = [slug for slug in slugs if slug in expected]
            rank = next(
                (index + 1 for index, slug in enumerate(slugs) if slug in expected), 0,
            )
            records.append({
                "id": item["id"],
                "category": item["category"],
                "query": item["query"],
                "expected": sorted(expected),
                "forbidden": sorted(forbidden),
                "top": slugs[:top_k],
                "hit": bool(hit_slugs),
                "rank": rank,
                "recall": len(set(hit_slugs)) / len(expected) if expected else None,
                "mrr": 1.0 / rank if rank else (0.0 if expected else None),
                "negative_pass": (
                    not (forbidden & set(slugs)) if not expected else None
                ),
            })
        results[mode] = {
            "records": records,
            "latency_ms": {
                "avg": round(statistics.fmean(latencies), 2),
                "p95": round(_percentile(latencies, 95), 2),
                "max": round(max(latencies), 2),
            },
        }
    return results


def _percentile(values: list[float], pct: float) -> float:
    """线性插值分位数（样本量小，避免依赖 numpy）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * pct / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _summarize(results: dict[str, Any]) -> dict[str, Any]:
    """汇总 Recall@5 / MRR@5 / 负例通过率，并按类别拆分。"""
    summary: dict[str, Any] = {}
    for mode, payload in results.items():
        records = payload["records"]
        positive = [r for r in records if r["expected"]]
        negative = [r for r in records if not r["expected"]]
        by_category: dict[str, dict[str, float]] = {}
        for category in sorted({r["category"] for r in records}):
            rows = [r for r in records if r["category"] == category]
            pos = [r for r in rows if r["expected"]]
            neg = [r for r in rows if not r["expected"]]
            by_category[category] = {
                "recall@5": round(statistics.fmean([r["recall"] for r in pos]), 4) if pos else None,
                "mrr@5": round(statistics.fmean([r["mrr"] for r in pos]), 4) if pos else None,
                "negative_pass": (
                    round(statistics.fmean([1.0 if r["negative_pass"] else 0.0 for r in neg]), 4)
                    if neg else None
                ),
            }
        summary[mode] = {
            "recall@5": round(statistics.fmean([r["recall"] for r in positive]), 4) if positive else None,
            "mrr@5": round(statistics.fmean([r["mrr"] for r in positive]), 4) if positive else None,
            "negative_pass": (
                round(statistics.fmean([1.0 if r["negative_pass"] else 0.0 for r in negative]), 4)
                if negative else None
            ),
            "positive_queries": len(positive),
            "negative_queries": len(negative),
            "latency_ms": payload["latency_ms"],
            "by_category": by_category,
        }
    return summary


def _print_report(summary: dict[str, Any], results: dict[str, Any], top_k: int) -> None:
    """打印对比表与典型查询的 Top-K。"""
    print(f"\n=== 检索模式对比（Top-{top_k}）===")
    header = f"{'模式':<10}{'Recall@5':>10}{'MRR@5':>9}{'负例通过':>10}{'平均延迟':>12}{'P95 延迟':>12}"
    print(header)
    print("-" * len(header))
    for mode in MODES:
        row = summary[mode]
        print(
            f"{mode:<10}{row['recall@5']:>10.4f}{row['mrr@5']:>9.4f}"
            f"{row['negative_pass']:>10.4f}{row['latency_ms']['avg']:>11.1f}ms"
            f"{row['latency_ms']['p95']:>11.1f}ms"
        )

    print("\n=== 分类别 ===")
    categories = sorted({c for mode in MODES for c in summary[mode]["by_category"]})
    for category in categories:
        parts = []
        for mode in MODES:
            cell = summary[mode]["by_category"].get(category, {})
            recall = cell.get("recall@5")
            negative = cell.get("negative_pass")
            value = f"{recall:.3f}" if recall is not None else (
                f"neg={negative:.3f}" if negative is not None else "-"
            )
            parts.append(f"{mode}={value}")
        print(f"  {category:<12}" + "  ".join(parts))

    samples = [q for q in results["hybrid"]["records"] if q["category"] in ("semantic", "similar", "permission")]
    print("\n=== 典型查询 Top-5（hybrid）===")
    for item in samples[:6]:
        print(f"  [{item['id']}/{item['category']}] {item['query']}")
        print(f"    expected={item['expected']} forbidden={item['forbidden']}")
        for mode in MODES:
            record = next(r for r in results[mode]["records"] if r["id"] == item["id"])
            print(f"    {mode:<8} {record['top']}")


def main() -> int:
    """执行评测并落盘 JSON 结果。"""
    from aiive.config import settings
    from aiive.memory.vector_projection import embedding_model_identity

    if not settings.aiive_knowledge_vector_enabled:
        print("错误：AIIVE_KNOWLEDGE_VECTOR_ENABLED 未开启，无法评测向量与混合检索。", file=sys.stderr)
        return 2

    corpus = json.loads((DATA_DIR / "corpus.json").read_text(encoding="utf-8"))
    dataset = json.loads((DATA_DIR / "queries.json").read_text(encoding="utf-8"))
    top_k = int(dataset.get("top_k", 5))

    _ensure_database()
    engine = create_engine(EVAL_DB_URL)
    _prepare_schema(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    workdir = Path(os.environ.get("TEMP", "/tmp")) / "aiive_rag_eval_corpus"
    workdir.mkdir(parents=True, exist_ok=True)

    db = session_factory()
    try:
        slug_map = _seed_corpus(db, corpus, workdir)
        warmup_started = time.perf_counter()
        _RUNNERS["vector"](db, "预热查询", top_k)
        warmup_ms = (time.perf_counter() - warmup_started) * 1000.0
        results = _evaluate(db, dataset["queries"], slug_map, top_k)
        summary = _summarize(results)
        _print_report(summary, results, top_k)
        payload = {
            "embedding_model": embedding_model_identity(),
            "embedding_dimensions": settings.aiive_embedding_dimensions,
            "min_similarity": settings.aiive_knowledge_vector_min_similarity,
            "top_k": top_k,
            "warmup_ms": round(warmup_ms, 2),
            "summary": summary,
            "results": results,
        }
        output = DATA_DIR / "last_run.json"
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {output}")
        return 0
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
