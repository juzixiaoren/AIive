"""知识库维护：一致性扫描与幂等清理。

维护范围严格限定在 AIive 管理的派生数据内：
- `documents` 行（软删除墓碑、被取代版本）
- `chunks` 行（派生分块）
- 对象存储 `knowledge-documents` 桶内的内容寻址原文

**绝不触碰 `source_path` 指向的用户原始文件。**

所有操作都是幂等的：扫描结果由当前库状态重新推导，重复执行不会重复删除；
每一步都可中断（每批 commit），下次运行从当前状态继续。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from aiive.db.models import Chunk, Document, OutboxJob
from aiive.knowledge.ingestor import (
    DOCUMENT_STATUSES,
    INACTIVE_STATUSES,
    KNOWLEDGE_BUCKET,
    RECOVERABLE_STATUSES,
    KnowledgeIngestor,
)
from aiive.storage import object_store
from aiive.storage.object_store import ObjectRef

logger = logging.getLogger(__name__)

# superseded 文档的原文对象保留期：期内可回到旧版本，过期后由维护任务回收
SUPERSEDED_OBJECT_RETENTION_DAYS = 30
# 单次 apply 每个类别的处理上限（保证可中断、可续跑）
DEFAULT_BATCH_LIMIT = 50
# 对象扫描安全上限：避免异常大的桶让单次扫描失控
OBJECT_SCAN_LIMIT = 50_000
# 只回收"写入已有一段时间"的对象：避免与并发摄取（已写对象、尚未提交）竞态误删
MIN_ORPHAN_OBJECT_AGE_SECONDS = 300

ISSUE_ORPHAN_CHUNKS = "orphan_chunks"
ISSUE_DUPLICATE_CHUNKS = "duplicate_chunks"
ISSUE_STALE_DOCUMENT_CHUNKS = "stale_document_chunks"
ISSUE_MISSING_CHUNKS = "missing_chunks"
ISSUE_MISSING_OBJECT = "missing_object"
ISSUE_ORPHAN_OBJECTS = "orphan_objects"
ISSUE_EXPIRED_SUPERSEDED_OBJECTS = "expired_superseded_objects"

ISSUE_CODES: tuple[str, ...] = (
    ISSUE_ORPHAN_CHUNKS,
    ISSUE_DUPLICATE_CHUNKS,
    ISSUE_STALE_DOCUMENT_CHUNKS,
    ISSUE_MISSING_CHUNKS,
    ISSUE_MISSING_OBJECT,
    ISSUE_ORPHAN_OBJECTS,
    ISSUE_EXPIRED_SUPERSEDED_OBJECTS,
)


def _now() -> datetime:
    """返回当前 UTC 时间。"""
    return datetime.now(timezone.utc)


@dataclass
class KnowledgeIssue:
    """单项一致性问题：代码、数量与少量样本标识。"""

    code: str
    count: int = 0
    sample: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 API 返回结构。"""
        return {"code": self.code, "count": self.count, "sample": list(self.sample)}


@dataclass
class KnowledgeMaintenanceReport:
    """知识库维护报告（只读扫描结果）。"""

    generated_at: str
    status_counts: dict[str, int]
    chunk_total: int
    issues: list[KnowledgeIssue]
    last_maintenance_at: str | None = None

    @property
    def repairable(self) -> int:
        """仍待清理的问题项总数。"""
        return sum(issue.count for issue in self.issues)

    @property
    def healthy(self) -> bool:
        """是否没有待清理问题。"""
        return self.repairable == 0

    def to_dict(self) -> dict[str, Any]:
        """序列化为 API 返回结构。"""
        return {
            "generated_at": self.generated_at,
            "status_counts": dict(self.status_counts),
            "chunk_total": self.chunk_total,
            "issues": [issue.to_dict() for issue in self.issues],
            "repairable": self.repairable,
            "healthy": self.healthy,
            "last_maintenance_at": self.last_maintenance_at,
        }


# ── 对象存储辅助 ──────────────────────────────────────────────────


def _bucket_root() -> Path:
    """返回知识原文桶的本地根目录。"""
    return object_store.ROOT / KNOWLEDGE_BUCKET


def _is_content_addressed_key(key: str) -> bool:
    """判断键是否是本模块写入的内容寻址键（sha256/xx/<64 位十六进制>）。"""
    parts = key.split("/")
    if len(parts) != 3 or parts[0] != "sha256":
        return False
    digest = parts[2]
    return len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)


def _iter_object_files() -> Iterator[tuple[str, Path]]:
    """遍历桶内的内容寻址对象文件，产出 (key, path)。

    跳过回收站、临时文件、隐藏目录与刚写入的对象（`MIN_ORPHAN_OBJECT_AGE_SECONDS`），
    避免把维护动作自己的中间产物、或并发摄取尚未提交的原文当成孤儿对象。
    """
    root = _bucket_root()
    if not root.is_dir():
        return
    scanned = 0
    fresh_cutoff = time.time() - MIN_ORPHAN_OBJECT_AGE_SECONDS
    for path in sorted(root.rglob("*")):
        if scanned >= OBJECT_SCAN_LIMIT:
            logger.warning("知识原文对象扫描达到上限 %s，剩余对象下轮继续", OBJECT_SCAN_LIMIT)
            return
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        key = relative.as_posix()
        if not _is_content_addressed_key(key):
            continue
        try:
            if path.stat().st_mtime > fresh_cutoff:
                continue
        except OSError:
            logger.debug("对象状态读取失败，跳过：%s", key, exc_info=True)
            continue
        scanned += 1
        yield key, path


def _referenced_keys(db: Session) -> set[str]:
    """返回仍被文档记录引用的对象键集合。"""
    rows = (
        db.query(Document.object_key)
        .filter(
            Document.object_bucket == KNOWLEDGE_BUCKET,
            Document.object_key.isnot(None),
        )
        .all()
    )
    return {str(key) for (key,) in rows if key}


# ── 扫描 ──────────────────────────────────────────────────────────


def _status_counts(db: Session) -> dict[str, int]:
    """统计各生命周期状态的文档数量。"""
    counts: dict[str, int] = {status: 0 for status in sorted(DOCUMENT_STATUSES)}
    rows = db.query(Document.status, func.count(Document.id)).group_by(Document.status).all()
    total = 0
    for status, count in rows:
        counts[str(status)] = int(count)
        total += int(count)
    counts["total"] = total
    return counts


def _orphan_chunk_ids(db: Session, limit: int) -> list[str]:
    """返回没有对应文档记录的分块 id。"""
    rows = (
        db.query(Chunk.id)
        .outerjoin(Document, Chunk.document_id == Document.id)
        .filter(Document.id.is_(None))
        .limit(limit)
        .all()
    )
    return [str(row[0]) for row in rows]


def _stale_chunk_ids(db: Session, limit: int) -> list[str]:
    """返回失效文档（superseded / deleted）残留的分块 id。"""
    rows = (
        db.query(Chunk.id)
        .join(Document, Chunk.document_id == Document.id)
        .filter(Document.status.in_(tuple(INACTIVE_STATUSES)))
        .limit(limit)
        .all()
    )
    return [str(row[0]) for row in rows]


def _duplicate_chunk_groups(db: Session, limit: int) -> list[tuple[str, int, str, int]]:
    """返回 (document_id, chunk_index, 保留的 chunk_id, 冗余条数) 的重复分组。"""
    rows = (
        db.query(Chunk.document_id, Chunk.chunk_index, func.min(Chunk.id), func.count(Chunk.id))
        .group_by(Chunk.document_id, Chunk.chunk_index)
        .having(func.count(Chunk.id) > 1)
        .limit(limit)
        .all()
    )
    return [(str(row[0]), int(row[1]), str(row[2]), int(row[3]) - 1) for row in rows]


def _missing_chunk_document_ids(db: Session, limit: int) -> list[str]:
    """返回可修复但完全没有分块的文档 id。"""
    rows = (
        db.query(Document.id)
        .filter(
            Document.status.in_(tuple(RECOVERABLE_STATUSES)),
            ~Document.id.in_(db.query(Chunk.document_id)),
        )
        .limit(limit)
        .all()
    )
    return [str(row[0]) for row in rows]


def _missing_object_document_ids(db: Session) -> list[str]:
    """返回对象引用指向的文件在本地桶中不存在的活跃文档 id。"""
    rows = (
        db.query(Document.id, Document.object_bucket, Document.object_key)
        .filter(
            Document.object_bucket == KNOWLEDGE_BUCKET,
            Document.object_key.isnot(None),
            Document.status.in_(tuple(RECOVERABLE_STATUSES)),
        )
        .all()
    )
    missing: list[str] = []
    for document_id, bucket, key in rows:
        if not bucket or not key:
            continue
        if not object_store.exists(ObjectRef(str(bucket), str(key))):
            missing.append(str(document_id))
    return missing


def _orphan_object_keys(db: Session) -> list[str]:
    """返回桶内没有被任何文档记录引用的内容寻址对象键。"""
    referenced = _referenced_keys(db)
    return [key for key, _path in _iter_object_files() if key not in referenced]


def _expired_superseded_ids(db: Session, limit: int) -> list[str]:
    """返回超过保留期、可回收原文对象的 superseded 文档 id。"""
    cutoff = _now() - timedelta(days=SUPERSEDED_OBJECT_RETENTION_DAYS)
    rows = (
        db.query(Document.id)
        .filter(
            Document.status == "superseded",
            Document.object_key.isnot(None),
            Document.updated_at <= cutoff,
        )
        .limit(limit)
        .all()
    )
    return [str(row[0]) for row in rows]


def _last_maintenance_at(db: Session) -> str | None:
    """返回最近一次成功的知识库维护完成时间。"""
    job = (
        db.query(OutboxJob)
        .filter(
            OutboxJob.job_type == "knowledge_maintenance",
            OutboxJob.status == "completed",
            OutboxJob.terminal_at.isnot(None),
        )
        .order_by(OutboxJob.terminal_at.desc())
        .first()
    )
    if job is None or job.terminal_at is None:
        return None
    return job.terminal_at.isoformat()


def scan_knowledge_base(
    db: Session, *, limit: int = DEFAULT_BATCH_LIMIT
) -> KnowledgeMaintenanceReport:
    """只读扫描知识库，返回一致性问题报告（不做任何修改）。"""
    sample_limit = max(1, min(int(limit), 200))
    orphan_chunks = _orphan_chunk_ids(db, 1000)
    stale_chunks = _stale_chunk_ids(db, 1000)
    duplicate_groups = _duplicate_chunk_groups(db, 1000)
    missing_chunks = _missing_chunk_document_ids(db, 1000)
    missing_objects = _missing_object_document_ids(db)
    orphan_objects = _orphan_object_keys(db)
    expired = _expired_superseded_ids(db, 1000)
    issues = [
        KnowledgeIssue(ISSUE_ORPHAN_CHUNKS, len(orphan_chunks), orphan_chunks[:sample_limit]),
        KnowledgeIssue(ISSUE_DUPLICATE_CHUNKS, sum(group[3] for group in duplicate_groups),
                       [f"{group[0]}#{group[1]}" for group in duplicate_groups[:sample_limit]]),
        KnowledgeIssue(ISSUE_STALE_DOCUMENT_CHUNKS, len(stale_chunks), stale_chunks[:sample_limit]),
        KnowledgeIssue(ISSUE_MISSING_CHUNKS, len(missing_chunks), missing_chunks[:sample_limit]),
        KnowledgeIssue(ISSUE_MISSING_OBJECT, len(missing_objects), missing_objects[:sample_limit]),
        KnowledgeIssue(ISSUE_ORPHAN_OBJECTS, len(orphan_objects), orphan_objects[:sample_limit]),
        KnowledgeIssue(ISSUE_EXPIRED_SUPERSEDED_OBJECTS, len(expired), expired[:sample_limit]),
    ]
    return KnowledgeMaintenanceReport(
        generated_at=_now().isoformat(),
        status_counts=_status_counts(db),
        chunk_total=int(db.query(func.count(Chunk.id)).scalar() or 0),
        issues=issues,
        last_maintenance_at=_last_maintenance_at(db),
    )


# ── 清理 ──────────────────────────────────────────────────────────


def _purge_chunk_ids(db: Session, chunk_ids: list[str]) -> int:
    """按 id 批量删除分块。"""
    if not chunk_ids:
        return 0
    return int(
        db.query(Chunk).filter(Chunk.id.in_(chunk_ids)).delete(synchronize_session=False)
    )


def _purge_stale_document_chunks(db: Session, limit: int) -> int:
    """清空失效文档残留的分块（保证旧版本永不被召回）。"""
    return _purge_chunk_ids(db, _stale_chunk_ids(db, limit))


def _purge_orphan_chunks(db: Session, limit: int) -> int:
    """删除没有所属文档的孤儿分块。"""
    return _purge_chunk_ids(db, _orphan_chunk_ids(db, limit))


def _purge_duplicate_chunks(db: Session, limit: int) -> int:
    """同一 (document_id, chunk_index) 只保留最早一条分块。"""
    removed = 0
    for document_id, chunk_index, keep_id, _redundant in _duplicate_chunk_groups(db, limit):
        removed += int(
            db.query(Chunk)
            .filter(
                Chunk.document_id == document_id,
                Chunk.chunk_index == chunk_index,
                Chunk.id != keep_id,
            )
            .delete(synchronize_session=False)
        )
    return removed


def _rebuild_missing_chunks(db: Session, limit: int) -> tuple[int, int]:
    """对分块缺失的活跃文档执行重建，返回 (成功数, 失败数)。

    每篇文档在独立保存点内重建：单篇失败只回滚该篇并标记 `index_failed`，
    不会污染整批事务，也不会把失败标记为成功。
    """
    ingestor = KnowledgeIngestor(db)
    rebuilt = 0
    failed = 0
    for document_id in _missing_chunk_document_ids(db, limit):
        try:
            with db.begin_nested():
                ingestor.reindex(document_id, mark_failure=False)
            rebuilt += 1
        except Exception:
            failed += 1
            logger.warning("知识文档重建失败，保留可恢复状态：%s", document_id, exc_info=True)
            document = db.get(Document, document_id)
            if document is not None:
                document.status = "index_failed"
                document.updated_at = _now()
    return rebuilt, failed


def _mark_missing_objects(db: Session) -> int:
    """把原文对象已丢失的文档标记为 source_unavailable。"""
    marked = 0
    for document_id in _missing_object_document_ids(db):
        document = db.get(Document, document_id)
        if document is None or document.status == "source_unavailable":
            continue
        document.status = "source_unavailable"
        document.updated_at = _now()
        marked += 1
    return marked


def _release_expired_superseded_objects(db: Session, limit: int) -> int:
    """回收超过保留期的 superseded 文档原文对象。"""
    ingestor = KnowledgeIngestor(db)
    released = 0
    for document_id in _expired_superseded_ids(db, limit):
        document = db.get(Document, document_id)
        if document is None:
            continue
        if ingestor.release_object(document):
            released += 1
    return released


def _remove_orphan_objects(db: Session, limit: int) -> int:
    """把没有被任何文档引用的内容寻址对象移入回收站。"""
    referenced = _referenced_keys(db)
    removed = 0
    for key, _path in _iter_object_files():
        if removed >= limit:
            break
        if key in referenced:
            continue
        try:
            object_store.delete(ObjectRef(KNOWLEDGE_BUCKET, key))
            removed += 1
        except Exception:
            logger.warning("孤儿对象回收失败：%s", key, exc_info=True)
    return removed


def apply_report(
    db: Session, report: KnowledgeMaintenanceReport, *, limit: int = DEFAULT_BATCH_LIMIT
) -> dict[str, Any]:
    """按扫描报告执行幂等清理，返回本次实际完成的动作计数。

    调用方负责 commit；`changed=False` 表示本轮没有任何进展（例如重建持续失败），
    调用方应停止循环并把剩余问题留在报告里，而不是继续空转。
    """
    limit = max(1, int(limit))
    actions: dict[str, Any] = {
        "stale_chunks_purged": _purge_stale_document_chunks(db, limit),
        "orphan_chunks_purged": _purge_orphan_chunks(db, limit),
        "duplicate_chunks_purged": _purge_duplicate_chunks(db, limit),
    }
    rebuilt, failed = _rebuild_missing_chunks(db, limit)
    actions["documents_rebuilt"] = rebuilt
    actions["documents_rebuild_failed"] = failed
    actions["documents_marked_unavailable"] = _mark_missing_objects(db)
    actions["superseded_objects_released"] = _release_expired_superseded_objects(db, limit)
    actions["orphan_objects_removed"] = _remove_orphan_objects(db, limit)
    db.flush()
    # 重建失败不算"有进展"：否则持续失败的文档会让维护任务无限空转
    progress_keys = (
        "stale_chunks_purged",
        "orphan_chunks_purged",
        "duplicate_chunks_purged",
        "documents_rebuilt",
        "documents_marked_unavailable",
        "superseded_objects_released",
        "orphan_objects_removed",
    )
    actions["changed"] = any(int(actions[key]) > 0 for key in progress_keys)
    actions["report"] = report.to_dict()
    return actions
