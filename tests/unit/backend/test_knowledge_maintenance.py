"""知识库维护闭环测试：文档生命周期、一致性扫描、幂等清理与 Outbox Handler。

全部用例离线运行：对象存储指向 tmp_path，不访问网络、不需要 API Key。
"""

import hashlib
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from aiive.db.models import Chunk, Document, OutboxJob
from aiive.knowledge import maintenance as maintenance_mod
from aiive.knowledge.ingestor import KnowledgeIngestor, search_chunks
from aiive.knowledge.maintenance import (
    ISSUE_EXPIRED_SUPERSEDED_OBJECTS,
    ISSUE_MISSING_CHUNKS,
    ISSUE_MISSING_OBJECT,
    ISSUE_ORPHAN_CHUNKS,
    ISSUE_ORPHAN_OBJECTS,
    ISSUE_STALE_DOCUMENT_CHUNKS,
    apply_report,
    scan_knowledge_base,
)
from aiive.storage import object_store
from aiive.storage.object_store import ObjectRef
from aiive.tools.safe_delete import get_scope_registry
from aiive.worker.handlers_knowledge import handle_knowledge_maintenance
from aiive.worker.job_types import ENABLED_OUTBOX_JOB_TYPES
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome


@pytest.fixture
def store(tmp_path, monkeypatch):
    """把对象存储根目录隔离到 tmp_path，并让 safe_delete 认可该范围。"""
    root = tmp_path / "object_store"
    monkeypatch.setattr(object_store, "ROOT", root)
    registry = get_scope_registry()
    previous = registry.get_scope_root("object_store")
    registry.register("object_store", root)
    try:
        yield root
    finally:
        if previous is not None:
            registry.register("object_store", previous)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _ingest(db, path: Path) -> dict:
    result = KnowledgeIngestor(db).ingest(str(path))
    db.flush()
    return result


def _issue(report, code: str) -> int:
    for item in report.issues:
        if item.code == code:
            return item.count
    raise AssertionError(f"报告缺少问题项：{code}")


def _write_orphan_object(store: Path, payload: bytes) -> Path:
    """写入一个已过保留年龄、无人引用的内容寻址对象。"""
    digest = hashlib.sha256(payload).hexdigest()
    path = store / "knowledge-documents" / "sha256" / digest[:2] / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    stale = time.time() - maintenance_mod.MIN_ORPHAN_OBJECT_AGE_SECONDS - 60
    os.utime(path, (stale, stale))
    return path


def _make_claim(operation_id: str = "knowledge_maintenance:all:1:test") -> ClaimedJob:
    return ClaimedJob(
        id=str(uuid.uuid4()),
        job_type="knowledge_maintenance",
        payload={"schema_version": 1, "operation_id": operation_id},
        trace_id=None,
        retry_count=0,
        max_retries=3,
        claim_token=str(uuid.uuid4()),
        schema_version=1,
        worker_id="test-worker",
    )


class TestDocumentLifecycle:
    """导入 / 更新 / 删除 / 重索引的文档生命周期。"""

    def test_content_change_supersedes_previous_version(self, db_session, store, tmp_path):
        """同一路径内容变化时旧版本失效，检索不再命中旧内容。"""
        source = _write(tmp_path / "note.md", "alpha 旧版本内容")
        first = _ingest(db_session, source)

        _write(source, "beta 新版本内容")
        second = _ingest(db_session, source)

        old = db_session.get(Document, first["document_id"])
        new = db_session.get(Document, second["document_id"])
        assert new is not None and old is not None
        assert second["document_id"] != first["document_id"]
        assert old.status == "superseded"
        assert old.superseded_by == second["document_id"]
        assert new.status == "indexed"
        assert db_session.query(Chunk).filter(Chunk.document_id == old.id).count() == 0
        assert db_session.query(Chunk).filter(Chunk.document_id == new.id).count() > 0
        assert search_chunks(db_session, "alpha", 5) == []
        assert [item["document_id"] for item in search_chunks(db_session, "beta", 5)] == [new.id]

    def test_content_reverting_revives_previous_version(self, db_session, store, tmp_path):
        """内容回到历史版本时复活原记录，当前版本被取代。"""
        source = _write(tmp_path / "revert.md", "版本一的内容")
        first = _ingest(db_session, source)
        _write(source, "版本二的内容")
        second = _ingest(db_session, source)

        _write(source, "版本一的内容")
        third = _ingest(db_session, source)
        db_session.flush()

        assert third["duplicate"] is True
        assert third["revived"] is True
        assert third["document_id"] == first["document_id"]
        assert db_session.get(Document, first["document_id"]).status == "indexed"
        assert db_session.get(Document, second["document_id"]).status == "superseded"
        assert search_chunks(db_session, "版本二", 5) == []
        assert [item["document_id"] for item in search_chunks(db_session, "版本一", 5)] == [
            first["document_id"],
        ]

    def test_reingest_same_content_is_idempotent(self, db_session, store, tmp_path):
        """内容未变化时复用同一文档，不产生新版本。"""
        source = _write(tmp_path / "same.txt", "稳定内容")
        first = _ingest(db_session, source)
        second = _ingest(db_session, source)

        assert second["duplicate"] is True
        assert second["document_id"] == first["document_id"]
        assert second["superseded"] == []
        assert db_session.query(Document).count() == 1

    def test_active_source_path_partial_unique_index(self, db_session):
        """回归：同一 source_path 同时只允许一条活跃文档（schema 级护栏）。

        这是"并发导入同路径不同内容 → 两个活跃版本"竞态的根治手段：部分唯一索引
        让后插入方直接失败，应用层无法在 READ COMMITTED 下可靠拦截。
        """
        source_path = "/kb/partial-unique.md"
        db_session.add(Document(
            id="doc-active-a", source_path=source_path,
            content_hash="h1" + "a" * 62, status="indexed",
            mime_type="text/plain", title="第一版",
        ))
        db_session.flush()

        savepoint = db_session.begin_nested()
        db_session.add(Document(
            id="doc-active-b", source_path=source_path,
            content_hash="h2" + "b" * 62, status="indexed",
            mime_type="text/plain", title="第二版",
        ))
        with pytest.raises(IntegrityError):
            db_session.flush()
        savepoint.rollback()

        assert db_session.query(Document).filter_by(source_path=source_path).count() == 1

    def test_inactive_documents_do_not_occupy_active_slot(self, db_session):
        """回归：失效态（superseded/deleted）不占用活跃位，版本切换不受索引影响。"""
        source_path = "/kb/partial-unique-inactive.md"
        for doc_id, status in (
            ("doc-active-keep", "indexed"),
            ("doc-superseded", "superseded"),
            ("doc-deleted", "deleted"),
        ):
            db_session.add(Document(
                id=doc_id, source_path=source_path,
                content_hash="h" + doc_id[-6:] + "c" * 57, status=status,
                mime_type="text/plain", title=doc_id,
            ))
        db_session.flush()

        assert db_session.query(Document).filter_by(
            source_path=source_path, status="indexed"
        ).count() == 1
        assert db_session.query(Document).filter_by(source_path=source_path).count() == 3

    def test_ingest_route_maps_concurrent_conflict_to_409(self, db_session, monkeypatch):
        """回归：并发导入同路径触发唯一索引时返回 409（可重试），而非 500。"""
        from fastapi import HTTPException

        from aiive.api import routes_knowledge as routes

        def _conflict(self, file_path):
            raise IntegrityError("INSERT", {}, Exception("duplicate key value"))

        monkeypatch.setattr(routes, "resolve_knowledge_path", lambda path: Path(path))
        monkeypatch.setattr(KnowledgeIngestor, "ingest", _conflict)

        with pytest.raises(HTTPException) as excinfo:
            routes.ingest(routes.IngestRequest(file_path="/kb/whatever.md"), db_session)
        assert excinfo.value.status_code == 409
        assert "并发导入冲突" in str(excinfo.value.detail)

    def test_delete_releases_derived_data_but_keeps_source_file(
        self, db_session, store, tmp_path
    ):
        """删除只清理 AIive 管理的分块与对象，不删除用户原始文件。"""
        source = _write(tmp_path / "delete-me.md", "待删除的知识内容")
        result = _ingest(db_session, source)
        document = db_session.get(Document, result["document_id"])
        assert document is not None
        object_path = ObjectRef(document.object_bucket, document.object_key).path
        assert object_path.is_file()

        outcome = KnowledgeIngestor(db_session).delete(result["document_id"])
        db_session.flush()

        assert outcome["chunks_removed"] > 0
        assert outcome["source_file_removed"] is False
        assert source.is_file()
        assert not object_path.is_file()
        assert db_session.query(Chunk).filter(
            Chunk.document_id == result["document_id"]
        ).count() == 0
        assert document.status == "deleted"
        assert document.deleted_at is not None
        assert search_chunks(db_session, "待删除", 5) == []

    def test_delete_is_idempotent(self, db_session, store, tmp_path):
        """重复删除返回 already_deleted，不报错也不重复清理。"""
        source = _write(tmp_path / "twice.md", "重复删除内容")
        result = _ingest(db_session, source)
        ingestor = KnowledgeIngestor(db_session)

        ingestor.delete(result["document_id"])
        db_session.flush()
        again = ingestor.delete(result["document_id"])

        assert again["ok"] is True
        assert again["already_deleted"] is True
        assert again["chunks_removed"] == 0

    def test_delete_missing_document_raises_lookup(self, db_session, store):
        """删除不存在的文档抛出 LookupError（路由映射为 404）。"""
        with pytest.raises(LookupError):
            KnowledgeIngestor(db_session).delete(str(uuid.uuid4()))

    def test_reindex_rejects_inactive_document(self, db_session, store, tmp_path):
        """失效文档不能被重索引，避免把旧版本内容复活。"""
        source = _write(tmp_path / "reindex.md", "第一版")
        first = _ingest(db_session, source)
        _write(source, "第二版")
        _ingest(db_session, source)

        with pytest.raises(ValueError):
            KnowledgeIngestor(db_session).reindex(first["document_id"])

    def test_duplicate_chunk_index_is_rejected(self, db_session, store, tmp_path):
        """同一文档的分块序号有唯一约束，杜绝重复分块。"""
        source = _write(tmp_path / "unique.md", "唯一分块")
        result = _ingest(db_session, source)
        chunk = db_session.query(Chunk).filter(
            Chunk.document_id == result["document_id"]
        ).first()
        assert chunk is not None

        db_session.add(Chunk(
            document_id=chunk.document_id,
            content="重复分块",
            line_start=0,
            line_end=0,
            chunk_index=chunk.chunk_index,
            token_estimate=1,
        ))
        with pytest.raises(IntegrityError):
            db_session.flush()


class TestMaintenanceScan:
    """一致性扫描：识别孤儿分块、失效分块、缺失分块与对象漂移。"""

    def test_healthy_base_reports_no_issue(self, db_session, store, tmp_path):
        """一致的知识库报告为 healthy。"""
        _ingest(db_session, _write(tmp_path / "ok.md", "健康内容"))
        report = scan_knowledge_base(db_session)

        assert report.healthy is True
        assert report.repairable == 0
        assert report.status_counts["indexed"] == 1
        assert report.chunk_total > 0

    def test_scan_detects_orphan_chunks(self, db_session, store):
        """没有所属文档的分块被识别为孤儿。"""
        db_session.add(Chunk(
            document_id=str(uuid.uuid4()),
            content="孤儿分块",
            line_start=0,
            line_end=0,
            chunk_index=0,
            token_estimate=1,
        ))
        db_session.flush()

        report = scan_knowledge_base(db_session)
        assert _issue(report, ISSUE_ORPHAN_CHUNKS) == 1
        assert report.healthy is False

    def test_apply_purges_orphan_chunks_idempotently(self, db_session, store):
        """孤儿分块被清理，重复执行不再有变化。"""
        db_session.add(Chunk(
            document_id=str(uuid.uuid4()),
            content="孤儿分块",
            line_start=0,
            line_end=0,
            chunk_index=0,
            token_estimate=1,
        ))
        db_session.flush()

        first = apply_report(db_session, scan_knowledge_base(db_session))
        db_session.flush()
        second = apply_report(db_session, scan_knowledge_base(db_session))

        assert first["orphan_chunks_purged"] == 1
        assert first["changed"] is True
        assert second["changed"] is False
        assert scan_knowledge_base(db_session).healthy is True

    def test_apply_purges_stale_document_chunks(self, db_session, store, tmp_path):
        """失效文档残留的分块会被清空，保证旧版本不被召回。"""
        source = _write(tmp_path / "stale.md", "旧版本内容")
        result = _ingest(db_session, source)
        document = db_session.get(Document, result["document_id"])
        assert document is not None
        document.status = "superseded"
        db_session.flush()

        report = scan_knowledge_base(db_session)
        assert _issue(report, ISSUE_STALE_DOCUMENT_CHUNKS) > 0
        assert search_chunks(db_session, "旧版本", 5) == []

        actions = apply_report(db_session, report)
        db_session.flush()

        assert actions["stale_chunks_purged"] > 0
        assert db_session.query(Chunk).filter(
            Chunk.document_id == result["document_id"]
        ).count() == 0

    def test_apply_rebuilds_missing_chunks(self, db_session, store, tmp_path):
        """分块缺失的活跃文档会从持久原文重建。"""
        source = _write(tmp_path / "rebuild.md", "需要重建的分块内容")
        result = _ingest(db_session, source)
        db_session.query(Chunk).filter(
            Chunk.document_id == result["document_id"]
        ).delete(synchronize_session=False)
        db_session.flush()

        report = scan_knowledge_base(db_session)
        assert _issue(report, ISSUE_MISSING_CHUNKS) == 1

        actions = apply_report(db_session, report)
        db_session.flush()

        assert actions["documents_rebuilt"] == 1
        assert actions["documents_rebuild_failed"] == 0
        assert db_session.query(Chunk).filter(
            Chunk.document_id == result["document_id"]
        ).count() > 0
        assert db_session.get(Document, result["document_id"]).status == "indexed"

    def test_rebuild_failure_keeps_recoverable_state(self, db_session, store, tmp_path):
        """原文对象损坏时重建失败，文档保留 index_failed 而不是被标记成功。"""
        source = _write(tmp_path / "broken.md", "原始内容")
        result = _ingest(db_session, source)
        document = db_session.get(Document, result["document_id"])
        assert document is not None
        object_path = ObjectRef(document.object_bucket, document.object_key).path
        object_path.write_bytes(b"tampered")
        db_session.query(Chunk).filter(
            Chunk.document_id == result["document_id"]
        ).delete(synchronize_session=False)
        db_session.flush()

        actions = apply_report(db_session, scan_knowledge_base(db_session))
        db_session.flush()

        assert actions["documents_rebuild_failed"] == 1
        assert actions["documents_rebuilt"] == 0
        assert actions["changed"] is False
        refreshed = db_session.get(Document, result["document_id"])
        assert refreshed is not None
        assert refreshed.status == "index_failed"
        assert db_session.query(Chunk).filter(
            Chunk.document_id == result["document_id"]
        ).count() == 0

    def test_apply_marks_missing_object_unavailable(self, db_session, store, tmp_path):
        """原文对象丢失的文档被标记为 source_unavailable。"""
        source = _write(tmp_path / "lost.md", "会丢失的对象")
        result = _ingest(db_session, source)
        document = db_session.get(Document, result["document_id"])
        assert document is not None
        ObjectRef(document.object_bucket, document.object_key).path.unlink()
        db_session.flush()

        report = scan_knowledge_base(db_session)
        assert _issue(report, ISSUE_MISSING_OBJECT) == 1

        actions = apply_report(db_session, report)
        db_session.flush()

        assert actions["documents_marked_unavailable"] == 1
        assert db_session.get(Document, result["document_id"]).status == "source_unavailable"
        # 已标记过的文档不会重复上报，避免维护任务空转
        assert _issue(scan_knowledge_base(db_session), ISSUE_MISSING_OBJECT) == 0

    def test_apply_removes_orphan_objects(self, db_session, store):
        """未被任何文档引用的内容寻址对象被回收。"""
        orphan = _write_orphan_object(store, b"orphan-bytes")

        report = scan_knowledge_base(db_session)
        assert _issue(report, ISSUE_ORPHAN_OBJECTS) == 1

        actions = apply_report(db_session, report)
        db_session.flush()

        assert actions["orphan_objects_removed"] == 1
        assert orphan.is_file() is False

    def test_fresh_object_is_not_collected(self, db_session, store):
        """刚写入的对象可能属于并发摄取，扫描不回收（防竞态误删）。"""
        digest = hashlib.sha256(b"in-flight-bytes").hexdigest()
        fresh = store / "knowledge-documents" / "sha256" / digest[:2] / digest
        fresh.parent.mkdir(parents=True, exist_ok=True)
        fresh.write_bytes(b"in-flight-bytes")

        assert _issue(scan_knowledge_base(db_session), ISSUE_ORPHAN_OBJECTS) == 0
        assert fresh.is_file() is True

    def test_apply_releases_expired_superseded_objects(self, db_session, store, tmp_path):
        """超过保留期的 superseded 原文对象被回收，文档记录保留。"""
        source = _write(tmp_path / "old-version.md", "旧版本原文")
        first = _ingest(db_session, source)
        _write(source, "新版本原文")
        _ingest(db_session, source)

        old = db_session.get(Document, first["document_id"])
        assert old is not None and old.status == "superseded"
        object_path = ObjectRef(old.object_bucket, old.object_key).path
        assert object_path.is_file()
        old.updated_at = datetime.now(timezone.utc) - timedelta(
            days=maintenance_mod.SUPERSEDED_OBJECT_RETENTION_DAYS + 1
        )
        db_session.flush()

        report = scan_knowledge_base(db_session)
        assert _issue(report, ISSUE_EXPIRED_SUPERSEDED_OBJECTS) == 1

        actions = apply_report(db_session, report)
        db_session.flush()

        assert actions["superseded_objects_released"] == 1
        assert object_path.is_file() is False
        refreshed = db_session.get(Document, first["document_id"])
        assert refreshed is not None
        assert refreshed.status == "superseded"
        assert refreshed.object_key is None


class TestMaintenanceHandler:
    """Outbox Handler：分批、幂等、无进展时停止空转。"""

    def test_job_type_is_enabled(self):
        """knowledge_maintenance 已进入 Outbox allowlist。"""
        assert "knowledge_maintenance" in ENABLED_OUTBOX_JOB_TYPES

    def test_handler_requires_operation_id(self, db_session):
        """缺少 operation_id 视为不可重试。"""
        claim = ClaimedJob(
            id=str(uuid.uuid4()),
            job_type="knowledge_maintenance",
            payload={},
            trace_id=None,
            retry_count=0,
            max_retries=3,
            claim_token=str(uuid.uuid4()),
            schema_version=1,
            worker_id="test-worker",
        )
        result = handle_knowledge_maintenance(claim)
        assert result.outcome == HandlerOutcome.NON_RETRYABLE

    def test_handler_completes_on_healthy_base(self, db_session, store, tmp_path):
        """一致的知识库直接完成。"""
        _ingest(db_session, _write(tmp_path / "healthy.md", "一致的内容"))
        result = handle_knowledge_maintenance(_make_claim())
        assert result.outcome == HandlerOutcome.COMPLETED

    def test_handler_repairs_and_is_repeatable(self, db_session, store):
        """一次运行清理孤儿分块，再次运行无变化且仍然成功。"""
        db_session.add(Chunk(
            document_id=str(uuid.uuid4()),
            content="孤儿分块",
            line_start=0,
            line_end=0,
            chunk_index=0,
            token_estimate=1,
        ))
        db_session.flush()

        first = handle_knowledge_maintenance(_make_claim())
        assert first.outcome == HandlerOutcome.COMPLETED
        assert "orphan_chunks_purged=1" in first.reason
        assert scan_knowledge_base(db_session).healthy is True

        second = handle_knowledge_maintenance(_make_claim())
        assert second.outcome == HandlerOutcome.COMPLETED
        assert "orphan_chunks_purged" not in second.reason

    def test_handler_stops_when_nothing_can_be_repaired(self, db_session, store, tmp_path):
        """重建持续失败时返回 COMPLETED(partial)，不无限循环。"""
        source = _write(tmp_path / "stuck.md", "无法重建的内容")
        result = _ingest(db_session, source)
        document = db_session.get(Document, result["document_id"])
        assert document is not None
        ObjectRef(document.object_bucket, document.object_key).path.write_bytes(b"bad")
        db_session.query(Chunk).filter(
            Chunk.document_id == result["document_id"]
        ).delete(synchronize_session=False)
        db_session.flush()

        outcome = handle_knowledge_maintenance(_make_claim())

        assert outcome.outcome == HandlerOutcome.COMPLETED
        assert outcome.terminal_reason == "knowledge_maintenance_partial"
        assert db_session.get(Document, result["document_id"]).status == "index_failed"


class TestMaintenanceScheduling:
    """调度入队：同窗口幂等，且终态任务不阻塞当天的新问题。"""

    def test_enqueue_is_idempotent_per_window(self, db_session):
        """同一 window_bucket 只创建一个待处理任务。"""
        from aiive.worker.scheduler_daemon import enqueue_knowledge_maintenance_job

        now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        first = enqueue_knowledge_maintenance_job(db_session, now=now)
        db_session.flush()
        second = enqueue_knowledge_maintenance_job(db_session, now=now)
        db_session.flush()

        assert first == second
        assert db_session.query(OutboxJob).filter(
            OutboxJob.job_type == "knowledge_maintenance"
        ).count() == 1

    def test_enqueue_allocates_suffix_after_terminal_job(self, db_session):
        """窗口内基础任务已终态时，补跑任务分配 #n 后缀（UNIQUE 不撞车）。"""
        from aiive.worker.scheduler_daemon import enqueue_knowledge_maintenance_job

        now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        base = enqueue_knowledge_maintenance_job(db_session, now=now)
        db_session.flush()
        job = db_session.query(OutboxJob).filter_by(operation_id=base).one()
        job.status = "completed"
        job.terminal_at = now
        db_session.flush()

        rerun = enqueue_knowledge_maintenance_job(db_session, now=now)
        db_session.flush()

        assert rerun == f"{base}#2"
        assert db_session.query(OutboxJob).filter(
            OutboxJob.job_type == "knowledge_maintenance"
        ).count() == 2

    def test_scanner_reenqueues_for_new_inconsistency_after_completion(
        self, db_session, store, tmp_path, monkeypatch
    ):
        """回归：当天任务完成后新出现的不一致必须能再次入队（即时修复语义）。"""
        from aiive.worker import scheduler_daemon

        now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        monkeypatch.setattr(scheduler_daemon, "datetime", _FixedDatetime)

        base = scheduler_daemon.enqueue_knowledge_maintenance_job(
            db_session, window_bucket="2026-10-23", now=now
        )
        db_session.flush()
        job = db_session.query(OutboxJob).filter_by(operation_id=base).one()
        job.status = "completed"
        job.terminal_at = now
        db_session.flush()

        # 当天晚些时候出现新的 DB 侧不一致：孤儿分块
        db_session.add(Chunk(
            document_id=str(uuid.uuid4()),
            content="当天新出现的孤儿分块",
            line_start=0, line_end=0, chunk_index=0, token_estimate=1,
        ))
        db_session.flush()

        scheduler_daemon._knowledge_maintenance_scanner_job()

        jobs = db_session.query(OutboxJob).filter(
            OutboxJob.job_type == "knowledge_maintenance"
        ).all()
        assert len(jobs) == 2
        rerun = [item for item in jobs if item.operation_id != base]
        assert len(rerun) == 1 and rerun[0].status == "pending"

    def test_scanner_skips_when_window_job_in_flight(self, db_session, store, monkeypatch):
        """回归：窗口内有 pending/running 任务（含 #n 补跑）时不重复入队。"""
        from aiive.worker import scheduler_daemon

        now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        monkeypatch.setattr(scheduler_daemon, "datetime", _FixedDatetime)

        base = scheduler_daemon.enqueue_knowledge_maintenance_job(
            db_session, window_bucket="2026-10-23", now=now
        )
        db_session.flush()
        scheduler_daemon._knowledge_maintenance_scanner_job()
        assert db_session.query(OutboxJob).filter(
            OutboxJob.job_type == "knowledge_maintenance"
        ).count() == 1

        # 基础任务终态、补跑任务在途：同样不新建
        job = db_session.query(OutboxJob).filter_by(operation_id=base).one()
        job.status = "completed"
        job.terminal_at = now
        db_session.flush()
        rerun = scheduler_daemon.enqueue_knowledge_maintenance_job(
            db_session, window_bucket="2026-10-23", now=now
        )
        db_session.flush()
        scheduler_daemon._knowledge_maintenance_scanner_job()
        assert rerun == f"{base}#2"
        assert db_session.query(OutboxJob).filter(
            OutboxJob.job_type == "knowledge_maintenance"
        ).count() == 2

    def test_needs_maintenance_detects_stale_chunks(self, db_session, store, tmp_path):
        """DB 侧存在失效分块时扫描器判定需要维护。"""
        from aiive.worker.scheduler_daemon import knowledge_needs_maintenance

        source = _write(tmp_path / "scan.md", "扫描判定内容")
        result = _ingest(db_session, source)
        assert knowledge_needs_maintenance(db_session) is False

        document = db_session.get(Document, result["document_id"])
        assert document is not None
        document.status = "deleted"
        db_session.flush()

        assert knowledge_needs_maintenance(db_session) is True


class TestMaintenanceApiWiring:
    """路由接线：导入 / 报告 / 清理 / 删除端点的行为。"""

    def test_delete_and_maintenance_endpoints(self, db_session, store, tmp_path, monkeypatch):
        """路由层只做参数转换，服务层可在离线库上直接验证。"""
        from fastapi import HTTPException

        from aiive.api import routes_knowledge as routes

        source = _write(tmp_path / "api.md", "接口层删除内容")
        monkeypatch.setattr(routes, "resolve_knowledge_path", lambda value: Path(value))

        ingested = routes.ingest(routes.IngestRequest(file_path=str(source)), db_session)
        assert ingested["ok"] is True

        report = routes.maintenance_report(db_session)
        assert report["healthy"] is True
        assert report["status_counts"]["indexed"] == 1

        applied = routes.run_maintenance(routes.MaintenanceRequest(apply=True), db_session)
        assert applied["applied"] is True
        assert applied["report"]["healthy"] is True

        assert len(routes.list_documents(50, False, db_session)["documents"]) == 1

        deleted = routes.delete_document(ingested["document_id"], db_session)
        assert deleted["already_deleted"] is False
        assert deleted["source_file_removed"] is False
        assert source.is_file()

        # 墓碑记录默认不出现在列表里，显式 include_inactive 才可见
        assert routes.list_documents(50, False, db_session)["documents"] == []
        assert len(routes.list_documents(50, True, db_session)["documents"]) == 1

        with pytest.raises(HTTPException) as excinfo:
            routes.delete_document(str(uuid.uuid4()), db_session)
        assert excinfo.value.status_code == 404

    def test_openapi_schema_builds(self):
        """路由可挂载且请求体可解析（防止可选 body 注解导致应用启动失败）。"""
        from fastapi import FastAPI

        from aiive.api import routes_knowledge as routes

        app = FastAPI()
        app.include_router(routes.router)
        paths = app.openapi()["paths"]

        assert "delete" in paths["/api/knowledge/{document_id}"]
        assert "get" in paths["/api/knowledge/maintenance"]
        assert "post" in paths["/api/knowledge/maintenance"]

    def test_read_source_corrupted_object_returns_409(self, db_session, store, tmp_path):
        """回归：原文哈希校验失败返回 409 而不是 500。"""
        from fastapi import HTTPException

        from aiive.api import routes_knowledge as routes

        source = _write(tmp_path / "corrupt.md", "原始内容")
        result = _ingest(db_session, source)
        document = db_session.get(Document, result["document_id"])
        assert document is not None
        ObjectRef(document.object_bucket, document.object_key).path.write_bytes(b"tampered")
        db_session.flush()

        with pytest.raises(HTTPException) as excinfo:
            routes.read_source(result["document_id"], db_session)
        assert excinfo.value.status_code == 409

    def test_reindex_missing_object_returns_409(self, db_session, store, tmp_path, monkeypatch):
        """回归：原文对象丢失时重索引返回 409 而不是 500。"""
        from fastapi import HTTPException

        from aiive.api import routes_knowledge as routes

        source = _write(tmp_path / "lost.md", "对象会丢失")
        result = _ingest(db_session, source)
        document = db_session.get(Document, result["document_id"])
        assert document is not None
        ObjectRef(document.object_bucket, document.object_key).path.unlink()
        db_session.flush()

        # 独立短会话在单测里即测试会话本身，屏蔽以避免提交外层事务
        monkeypatch.setattr(
            KnowledgeIngestor, "_mark_index_failed", staticmethod(lambda document_id: None)
        )
        with pytest.raises(HTTPException) as excinfo:
            routes.reindex(result["document_id"], db_session)
        assert excinfo.value.status_code == 409
