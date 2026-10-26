"""2 GiB 单 cgroup 实测：在容器内施加真实负载并采样内存。

负载阶段（与验收标准一一对应）：
1. startup —— 服务启动 + 模型加载 + 预热（由宿主脚本计时，这里记录首次推理延迟）
2. idle    —— 空闲 20 秒
3. ingest  —— 知识库重建（真实分块 + 真实向量）+ 记忆向量写入/回填
4. query   —— 真实混合检索，4 并发 × 全部标注查询

内存来源全部是 cgroup v2 的真实计数器（容器内即整套服务的 cgroup）：
memory.current / memory.peak / memory.events / memory.swap.current。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import fmean

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_ROOT = _REPO_ROOT / "backend"
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

CGROUP = Path("/sys/fs/cgroup")
SAMPLE_INTERVAL = 0.1
OUTPUT = Path("/app/.data/memtest/report.json")


def _read_int(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def _read_flat_keyed(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2:
                values[parts[0]] = int(parts[1])
    except OSError:
        return values
    return values


def cgroup_snapshot() -> dict[str, int]:
    """当前 cgroup 的内存快照（字节）。"""
    return {
        "current": _read_int(CGROUP / "memory.current"),
        "peak": _read_int(CGROUP / "memory.peak"),
        "swap_current": _read_int(CGROUP / "memory.swap.current"),
        "swap_peak": _read_int(CGROUP / "memory.swap.peak"),
        "anon": _read_flat_keyed(CGROUP / "memory.stat").get("anon", 0),
        "file": _read_flat_keyed(CGROUP / "memory.stat").get("file", 0),
    }


def rss_by_service() -> dict[str, int]:
    """按进程名聚合 RSS（KB），用于给出各服务的内存构成。"""
    try:
        out = subprocess.run(
            ["ps", "-eo", "rss=,comm="], capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except Exception:  # noqa: BLE001 - 采样失败不影响主流程
        return {}
    totals: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        rss, comm = parts
        try:
            totals[comm] = totals.get(comm, 0) + int(rss)
        except ValueError:
            continue
    return totals


class MemorySampler(threading.Thread):
    """后台每 100ms 采样一次 cgroup 内存，记录峰值与阶段归属。"""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._stop_event = threading.Event()
        self.phase = "startup"
        self.peak_by_phase: dict[str, int] = {}
        self.samples: list[tuple[float, str, int]] = []
        self.swap_peak = 0

    def run(self) -> None:
        while not self._stop_event.is_set():
            snap = cgroup_snapshot()
            now = time.time()
            self.samples.append((now, self.phase, snap["current"]))
            self.peak_by_phase[self.phase] = max(
                self.peak_by_phase.get(self.phase, 0), snap["current"],
            )
            self.swap_peak = max(self.swap_peak, snap["swap_current"])
            self._stop_event.wait(SAMPLE_INTERVAL)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5)

    @property
    def peak(self) -> int:
        return max((value for _, _, value in self.samples), default=0)


def _wait_backend(timeout: float = 180.0) -> float:
    """等待 backend 健康检查通过，返回等待耗时。"""
    import httpx

    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        try:
            if httpx.get("http://127.0.0.1:8000/health", timeout=2.0).status_code == 200:
                return time.perf_counter() - started
        except Exception:  # noqa: BLE001 - 启动中连接失败属正常，继续等待
            time.sleep(0.2)
        time.sleep(1.0)
    raise RuntimeError("backend 在超时内未就绪")


def main() -> int:
    from aiive.config import settings
    from aiive.db.models import Chunk, Document, KnowledgeChunkVector
    from aiive.knowledge.ingestor import KnowledgeIngestor, search_chunks
    from aiive.memory.vector_projection import (
        MemoryVectorProjectionService,
        embedding_model_identity,
    )
    from sqlalchemy import create_engine, func
    from sqlalchemy.orm import sessionmaker

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    sampler = MemorySampler()
    sampler.start()

    baseline = cgroup_snapshot()
    backend_ready_seconds = _wait_backend()
    sampler.phase = "idle"
    time.sleep(20)

    engine = create_engine(settings.database_url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    # ── 阶段：知识库重建 + 记忆向量写入 ──
    sampler.phase = "ingest"
    ingest_started = time.perf_counter()
    corpus = json.loads((_REPO_ROOT / "scripts/rag_eval/corpus.json").read_text(encoding="utf-8"))
    # 规模旋钮：把同一份语料复制成 N 个变体，模拟更大的知识库重建压力
    variants = int(os.environ.get("MEMTEST_DOC_VARIANTS", "1"))
    memory_target = int(os.environ.get("MEMTEST_MEMORIES", "30"))
    workdir = Path("/tmp/memtest_corpus")
    workdir.mkdir(parents=True, exist_ok=True)
    db = session_factory()
    try:
        for variant in range(variants):
            for doc in corpus["docs"]:
                path = workdir / f"{doc['slug']}-v{variant}.md"
                # 变体必须内容不同，否则会被内容哈希去重（这本身是正确行为，
                # 但会让"更大知识库"的压力测试失效）
                body = doc["content"] + f"\n\n第 {variant} 份副本：同步自同一来源的第 {variant} 版整理稿。\n"
                path.write_text(body, encoding="utf-8")
                KnowledgeIngestor(db).ingest(str(path))
                db.flush()
                if doc["status"] == "deleted":
                    document = db.query(Document).filter(
                        Document.source_path == str(path),
                    ).one()
                    KnowledgeIngestor(db).delete(document.id)
                    db.flush()
        db.commit()
        chunk_count = int(db.query(func.count(Chunk.id)).scalar() or 0)
        vector_count = int(db.query(func.count(KnowledgeChunkVector.chunk_id)).scalar() or 0)

        # 记忆向量写入（等价于历史回填的有限批次）：30 条记忆逐条投影
        import uuid
        from datetime import UTC, datetime

        from aiive.db.models import MemoryRecord

        projection = MemoryVectorProjectionService(db)
        memory_started = time.perf_counter()
        for index in range(memory_target):
            record = MemoryRecord(
                id=str(uuid.uuid4()), memory_type="preference",
                canonical_key=f"memtest.{index}", scope_type="global", scope_id=None,
                content=f"测试记忆 {index}：用户偏好第 {index} 项配置，用于向量写入压力测试。",
                lifecycle_state="active", validity_state="valid", confidence=0.9,
                importance=0.5, retention_policy="normal", record_version=1,
                pinned=False, stability="stable", observed_at=datetime.now(UTC),
                created_at=datetime.now(UTC),
            )
            db.add(record)
            db.flush()
            projection.refresh(record.id, record.record_version)
            db.flush()
        db.commit()
        memory_seconds = time.perf_counter() - memory_started
    finally:
        db.close()
    ingest_seconds = time.perf_counter() - ingest_started

    # ── 阶段：真实混合检索 + 少量并发 ──
    sampler.phase = "query"
    dataset = json.loads((_REPO_ROOT / "scripts/rag_eval/queries.json").read_text(encoding="utf-8"))
    queries = [item["query"] for item in dataset["queries"]]
    latencies: list[float] = []
    errors = 0
    lock = threading.Lock()

    def worker(batch: list[str]) -> None:
        nonlocal errors
        session = session_factory()
        try:
            for query in batch:
                started = time.perf_counter()
                try:
                    search_chunks(session, query, 5)
                except Exception:  # noqa: BLE001 - 记录失败次数即可
                    with lock:
                        errors += 1
                elapsed = (time.perf_counter() - started) * 1000.0
                with lock:
                    latencies.append(elapsed)
        finally:
            session.close()

    rounds = int(os.environ.get("MEMTEST_QUERY_ROUNDS", "4"))
    all_queries = queries * rounds
    concurrency = int(os.environ.get("MEMTEST_CONCURRENCY", "4"))
    batches = [all_queries[i::concurrency] for i in range(concurrency)]
    query_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(worker, batches))
    query_seconds = time.perf_counter() - query_started

    # 召回质量旁证：语义改写查询确实命中目标文档
    recall_probe = []
    db = session_factory()
    try:
        # 质量旁证：语义改写（问法与文档用词无字面重叠）应召回 ops-backup
        probe_query = "怎样把数据库恢复到某个时间点"
        hits = search_chunks(db, probe_query, 5)
        recall_probe = [
            {"document": Path(hit["source_path"]).stem, "score": hit.get("hybrid_score")}
            for hit in hits
        ]
    finally:
        db.close()

    sampler.phase = "cooldown"
    time.sleep(3)
    sampler.stop()

    final = cgroup_snapshot()
    events = _read_flat_keyed(CGROUP / "memory.events")
    limit_bytes = _read_int(CGROUP / "memory.max")
    rss = rss_by_service()
    sorted_latencies = sorted(latencies)

    def percentile(pct: float) -> float:
        if not sorted_latencies:
            return 0.0
        position = (len(sorted_latencies) - 1) * pct / 100.0
        lower = int(position)
        upper = min(lower + 1, len(sorted_latencies) - 1)
        weight = position - lower
        return sorted_latencies[lower] * (1 - weight) + sorted_latencies[upper] * weight

    report = {
        "embedding_model": embedding_model_identity(),
        "embedding_service": {
            "max_concurrent_requests": os.environ.get("TEI_MAX_CONCURRENCY"),
            "max_batch_tokens": os.environ.get("TEI_MAX_BATCH_TOKENS"),
            "client_batch_size": settings.aiive_embedding_batch_size,
            "base_url": settings.aiive_embedding_base_url,
        },
        "cgroup": {
            "limit_bytes": limit_bytes,
            "limit_mib": round(limit_bytes / 1024 / 1024, 1),
            "peak_bytes": final["peak"],
            "peak_mib": round(final["peak"] / 1024 / 1024, 1),
            "baseline_current_mib": round(baseline["current"] / 1024 / 1024, 1),
            "final_current_mib": round(final["current"] / 1024 / 1024, 1),
            "headroom_mib": round((limit_bytes - final["peak"]) / 1024 / 1024, 1),
            "peak_by_phase_mib": {
                phase: round(value / 1024 / 1024, 1)
                for phase, value in sampler.peak_by_phase.items()
            },
            "sampled_peak_mib": round(sampler.peak / 1024 / 1024, 1),
            "swap_current_mib": round(final["swap_current"] / 1024 / 1024, 2),
            "swap_peak_mib": round(max(sampler.swap_peak, final["swap_peak"]) / 1024 / 1024, 2),
            "memory_events": events,
            "anon_mib": round(final["anon"] / 1024 / 1024, 1),
            "file_mib": round(final["file"] / 1024 / 1024, 1),
        },
        "rss_mib": {
            name: round(value / 1024, 1) for name, value in sorted(
                rss.items(), key=lambda item: item[1], reverse=True,
            )[:8]
        },
        "phases": {
            "backend_ready_seconds": round(backend_ready_seconds, 2),
            "ingest_seconds": round(ingest_seconds, 2),
            "memory_write_seconds": round(memory_seconds, 2),
            "query_seconds": round(query_seconds, 2),
        },
        "data": {
            "document_variants": variants,
            "documents": len(corpus["docs"]) * variants,
            "chunks": chunk_count,
            "chunk_vectors": vector_count,
            "memory_records": memory_target,
        },
        "queries": {
            "total": len(all_queries),
            "concurrency": concurrency,
            "errors": errors,
            "avg_ms": round(fmean(latencies), 2) if latencies else 0.0,
            "p50_ms": round(percentile(50), 2),
            "p95_ms": round(percentile(95), 2),
            "max_ms": round(max(latencies), 2) if latencies else 0.0,
        },
        "recall_probe": recall_probe,
        "health": _health_probe(),
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({k: report[k] for k in ("cgroup", "phases", "queries", "data")},
                     ensure_ascii=False, indent=2))
    return 0


def _health_probe() -> dict[str, object]:
    """记录服务是否仍然健康（无异常重启的证据之一）。"""
    import httpx

    result: dict[str, object] = {}
    for name, url in (
        ("backend", "http://127.0.0.1:8000/health"),
        ("embedding", "http://127.0.0.1:8081/health"),
    ):
        try:
            result[name] = httpx.get(url, timeout=5.0).status_code
        except Exception as exc:  # noqa: BLE001 - 健康检查失败即记录原因
            result[name] = f"error: {exc.__class__.__name__}"
    return result


if __name__ == "__main__":
    raise SystemExit(main())
