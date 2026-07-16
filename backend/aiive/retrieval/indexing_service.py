"""Phase 5：检索索引刷新逻辑（retrieval_index_refresh 消费端）。

单一刷新作业：从 payload 取出 source_type/source_id，对「active + building」两个
generation 同时 upsert（或 tombstone）。upsert_entry 内部按 source_version fencing，
旧 Job 不会覆盖新数据（revision 3/4）。

下线类事件（forgotten / archived / superseded）或源已删除 → tombstone 该 source
在 generation 内所有 entry（清除 token、清空文本、is_searchable=False）。
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from aiive.db.models import EpochCheckpoint, MemoryRecord, SegmentSummary
from aiive.retrieval.retrieval_index import RetrievalIndexManager
from aiive.retrieval.retrieval_store import build_entry_fields

logger = logging.getLogger(__name__)

_TOMBSTONE_EVENTS: frozenset[str] = frozenset({
    "memory.forgotten",
    "memory.superseded",
})


def refresh_source(
    db: Session, source_type: str, source_id: str, event_type: str,
) -> tuple[int, int]:
    """对 active + building generation 刷新单个 source。

    返回 (upserted, tombstoned) 计数（仅统计 active generation 的影响）。
    """
    mgr = RetrievalIndexManager()
    active, building = mgr.get_active_and_building(db)
    if active is None:
        # 无 active generation，刷新无意义（rebuild 会全量覆盖）
        return 0, 0

    orm = _load_source(db, source_type, source_id)
    gens = [g for g in (active, building) if g is not None]

    upserted = 0
    tombstoned = 0
    for g in gens:
        fields = build_entry_fields(db, source_type, orm) if orm is not None else None
        if orm is None or fields is None:
            mgr.tombstone_by_source(db, g.index_version, source_type, source_id)
            tombstoned += 1
            continue
        if fields.get("is_forgotten") or event_type in _TOMBSTONE_EVENTS:
            mgr.tombstone_by_source(db, g.index_version, source_type, source_id)
            tombstoned += 1
            continue
        res = mgr.upsert_entry(
            db, g,
            source_type=fields["source_type"],
            source_id=fields["source_id"],
            source_version=fields["source_version"],
            source_hash=fields.get("source_hash"),
            title=fields["title"],
            search_text=fields["search_text"],
            snippet=fields["snippet"],
            scope_type=fields["scope_type"],
            scope_id=fields["scope_id"],
            canonical_key=fields.get("canonical_key"),
            lifecycle_state=fields.get("lifecycle_state"),
            validity_state=fields.get("validity_state"),
            retrieval_tier=fields["retrieval_tier"],
            thread_id=fields.get("thread_id"),
            epoch_id=fields.get("epoch_id"),
            segment_id=fields.get("segment_id"),
            memory_record_id=fields.get("memory_record_id"),
            metadata=fields.get("metadata") or {},
            created_source_at=fields.get("created_source_at"),
            updated_source_at=fields.get("updated_source_at"),
            tokens=fields["tokens"],
        )
        if res == "upserted" and g.index_version == active.index_version:
            upserted += 1
    return upserted, tombstoned


def _load_source(db: Session, source_type: str, source_id: str):
    if source_type == "memory_record":
        return db.get(MemoryRecord, source_id)
    if source_type == "segment_summary":
        return db.get(SegmentSummary, source_id)
    if source_type == "epoch_checkpoint":
        return db.get(EpochCheckpoint, source_id)
    return None
