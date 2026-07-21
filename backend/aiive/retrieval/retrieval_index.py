"""Phase 5：检索索引读写（RetrievalIndexManager）。

职责：
- 管理全局 generation（唯一 active 作为查询真相源）；
- Entry 的 upsert / tombstone（含 token posting 替换）；
- 倒排 posting 的 lexical 查询与 exact 查询；
- 全量 rebuild 的有界分批游标（供 index_rebuild 使用）。

语义约束：
- 旧 source_version 的 Job 不得覆盖新版本（fencing）；
- forgotten 的 Entry 走 tombstone（删除 token、清空文本、is_searchable=False）；
- 同 source 在 generation 内仅一个 is_current。
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from aiive.db.models import (
    EpochCheckpoint,
    MemoryRecord,
    RetrievalIndexEntry,
    RetrievalIndexGeneration,
    RetrievalIndexToken,
    SegmentSummary,
)
from aiive.retrieval.source_version import version_cmp

logger = logging.getLogger(__name__)


class RetrievalIndexManager:
    """检索索引的读写管理（无 LLM、无 embedding）。"""

    # ── Generation ──

    def get_active_generation(self, db: Session) -> RetrievalIndexGeneration | None:
        return (
            db.query(RetrievalIndexGeneration)
            .filter(RetrievalIndexGeneration.status == "active")
            .order_by(RetrievalIndexGeneration.index_version.desc())
            .first()
        )

    def get_active_and_building(
        self, db: Session
    ) -> tuple[RetrievalIndexGeneration | None, RetrievalIndexGeneration | None]:
        active = None
        building = None
        gens = (
            db.query(RetrievalIndexGeneration)
            .filter(RetrievalIndexGeneration.status.in_(("active", "building")))
            .order_by(RetrievalIndexGeneration.index_version.desc())
            .all()
        )
        for g in gens:
            if g.status == "active" and active is None:
                active = g
            elif g.status == "building" and building is None:
                building = g
        return active, building

    def next_index_version(self, db: Session) -> int:
        row = db.query(func.max(RetrievalIndexGeneration.index_version)).first()
        max_v = row[0] if row and row[0] is not None else 0
        return int(max_v) + 1

    def create_generation(
        self, db: Session, index_version: int, status: str = "building",
        source_cutoff: datetime | None = None, policy_version: str = "phase5.v1",
    ) -> RetrievalIndexGeneration:
        now = datetime.now(timezone.utc)
        gen = RetrievalIndexGeneration(
            index_version=index_version,
            status=status,
            build_started_at=now if status == "building" else None,
            source_cutoff=source_cutoff,
            policy_version=policy_version,
        )
        db.add(gen)
        db.flush()
        return gen

    def activate_generation(self, db: Session, index_version: int) -> None:
        """原子激活：将目标置 active，其余 active/retired 之间切换。"""
        now = datetime.now(timezone.utc)
        # 旧 active -> retired（同时记录 status_changed_at）
        db.query(RetrievalIndexGeneration).filter(
            RetrievalIndexGeneration.status == "active",
            RetrievalIndexGeneration.index_version != index_version,
        ).update({
            RetrievalIndexGeneration.status: "retired",
            RetrievalIndexGeneration.status_changed_at: now,
        }, synchronize_session=False)
        # 目标 -> active（同时记录 status_changed_at）
        db.query(RetrievalIndexGeneration).filter(
            RetrievalIndexGeneration.index_version == index_version,
        ).update({
            RetrievalIndexGeneration.status: "active",
            RetrievalIndexGeneration.activated_at: now,
            RetrievalIndexGeneration.build_completed_at: now,
            RetrievalIndexGeneration.status_changed_at: now,
        }, synchronize_session=False)
        db.flush()

    # ── Entry upsert / tombstone ──

    def upsert_entry(
        self, db: Session, generation: RetrievalIndexGeneration,
        *, source_type: str, source_id: str, source_version: str, source_hash: str | None,
        title: str, search_text: str, snippet: str,
        scope_type: str, scope_id: str | None, canonical_key: str | None,
        lifecycle_state: str | None, validity_state: str | None, retrieval_tier: str,
        thread_id: str | None, epoch_id: str | None, segment_id: str | None,
        memory_record_id: str | None, metadata: dict[str, Any],
        created_source_at: datetime | None, updated_source_at: datetime | None,
        tokens: list[tuple[str, str]],
    ) -> str:
        """upsert 一个 Entry + token posting（精确版本匹配 + 全局 demote）。

        流程：
        1. 按 source_version 精确查询 existing
        2. 命中 → 原地更新；未命中 → 用 version_cmp 做 fencing
        3. 旧 Job（payload version < 当前最新版本）→ skipped_stale
        4. 始终 demote 同 source/generation 下其他 current 行（最多一个 current）

        返回 "upserted" / "skipped_stale"。
        """
        iv = generation.index_version
        now_ts = datetime.now(timezone.utc)

        # 1. 精确版本匹配（不依赖字符串序，不走 desc() 排序）
        exact = (
            db.query(RetrievalIndexEntry)
            .filter(
                RetrievalIndexEntry.source_type == source_type,
                RetrievalIndexEntry.source_id == source_id,
                RetrievalIndexEntry.index_version == iv,
                RetrievalIndexEntry.source_version == source_version,
            )
            .first()
        )

        if exact is not None:
            # 精确命中 → 原地更新字段，并 demote 其他 current 行
            self._demote_other_currents(db, iv, source_type, source_id, exclude_id=exact.id)
            exact.source_hash = source_hash
            exact.title = title
            exact.search_text = search_text
            exact.snippet = snippet
            exact.scope_type = scope_type
            exact.scope_id = scope_id
            exact.canonical_key = canonical_key
            exact.lifecycle_state = lifecycle_state
            exact.validity_state = validity_state
            exact.retrieval_tier = retrieval_tier
            exact.thread_id = thread_id
            exact.epoch_id = epoch_id
            exact.segment_id = segment_id
            exact.memory_record_id = memory_record_id
            exact.extra_metadata = metadata
            exact.updated_source_at = updated_source_at
            exact.is_current = True
            exact.is_searchable = True
            exact.indexed_at = now_ts
            self._replace_tokens(db, exact.id, iv, tokens)
            return "upserted"

        # 2. 无精确命中 → fencing：用 version_cmp 数值比较，不用 source_version.desc()
        siblings = (
            db.query(RetrievalIndexEntry)
            .filter(
                RetrievalIndexEntry.source_type == source_type,
                RetrievalIndexEntry.source_id == source_id,
                RetrievalIndexEntry.index_version == iv,
            )
            .all()
        )

        def _version_key(v: str) -> int:
            try:
                return int(v)
            except (ValueError, TypeError):
                return 0

        max_sibling = max(siblings, key=lambda e: _version_key(e.source_version)) if siblings else None

        if max_sibling is not None and version_cmp(max_sibling.source_version, source_version) > 0:
            # 已有更新版本 → 旧 Job 不覆盖新数据
            return "skipped_stale"

        # 3. 创建新 entry，demote 所有同 source 的其他 current 行
        self._demote_other_currents(db, iv, source_type, source_id)
        entry = RetrievalIndexEntry(
            source_type=source_type,
            source_id=source_id,
            source_version=source_version,
            source_hash=source_hash,
            index_version=iv,
            title=title,
            search_text=search_text,
            snippet=snippet,
            scope_type=scope_type,
            scope_id=scope_id,
            canonical_key=canonical_key,
            lifecycle_state=lifecycle_state,
            validity_state=validity_state,
            retrieval_tier=retrieval_tier,
            thread_id=thread_id,
            epoch_id=epoch_id,
            segment_id=segment_id,
            memory_record_id=memory_record_id,
            extra_metadata=metadata,
            created_source_at=created_source_at,
            updated_source_at=updated_source_at,
            is_current=True,
            is_searchable=True,
        )
        db.add(entry)
        db.flush()
        self._replace_tokens(db, entry.id, iv, tokens)
        return "upserted"

    def _demote_other_currents(
        self, db: Session, index_version: int, source_type: str, source_id: str,
        exclude_id: str | None = None,
    ) -> None:
        """将同 (index_version, source_type, source_id) 下的其他 current 行置 False。"""
        q = db.query(RetrievalIndexEntry).filter(
            RetrievalIndexEntry.index_version == index_version,
            RetrievalIndexEntry.source_type == source_type,
            RetrievalIndexEntry.source_id == source_id,
            RetrievalIndexEntry.is_current == True,  # noqa: E712
        )
        if exclude_id is not None:
            q = q.filter(RetrievalIndexEntry.id != exclude_id)
        q.update({RetrievalIndexEntry.is_current: False}, synchronize_session=False)
        db.flush()

    def tombstone_entry(self, db: Session, entry_id: str) -> None:
        """forgotten：删除 token、清空文本与敏感 metadata、is_searchable=False。"""
        # 删除 posting
        db.query(RetrievalIndexToken).filter(
            RetrievalIndexToken.entry_id == entry_id
        ).delete(synchronize_session=False)
        entry = db.get(RetrievalIndexEntry, entry_id)
        if entry is not None:
            entry.title = ""
            entry.search_text = ""
            entry.snippet = ""
            entry.extra_metadata = {}
            entry.is_searchable = False
            entry.is_current = False
            entry.indexed_at = datetime.now(timezone.utc)
        db.flush()

    def tombstone_by_source(
        self, db: Session, index_version: int, source_type: str, source_id: str,
    ) -> None:
        entries = (
            db.query(RetrievalIndexEntry)
            .filter(
                RetrievalIndexEntry.index_version == index_version,
                RetrievalIndexEntry.source_type == source_type,
                RetrievalIndexEntry.source_id == source_id,
            )
            .all()
        )
        for e in entries:
            self.tombstone_entry(db, e.id)

    # ── token posting ──

    def _replace_tokens(
        self, db: Session, entry_id: str, index_version: int,
        tokens: list[tuple[str, str]],
    ) -> None:
        db.query(RetrievalIndexToken).filter(
            RetrievalIndexToken.entry_id == entry_id
        ).delete(synchronize_session=False)
        counts = Counter(tokens)
        for (tok, kind), tf in counts.items():
            db.add(RetrievalIndexToken(
                entry_id=entry_id,
                index_version=index_version,
                token=tok,
                token_kind=kind,
                term_frequency=tf,
            ))
        db.flush()

    def query_tokens(
        self, db: Session, index_version: int, tokens: list[str], top_k: int,
    ) -> list[tuple[str, int]]:
        """倒排 posting 聚合：返回 (entry_id, 命中数) 降序。"""
        if not tokens:
            return []
        rows = (
            db.query(
                RetrievalIndexToken.entry_id,
                func.count(RetrievalIndexToken.id).label("hits"),
            )
            .filter(
                RetrievalIndexToken.index_version == index_version,
                RetrievalIndexToken.token.in_(tokens),
            )
            .group_by(RetrievalIndexToken.entry_id)
            .order_by(desc("hits"))
            .limit(top_k)
            .all()
        )
        return [(r[0], int(r[1])) for r in rows]

    def fetch_entries_by_ids(
        self, db: Session, index_version: int, entry_ids: list[str],
    ) -> list[RetrievalIndexEntry]:
        if not entry_ids:
            return []
        return (
            db.query(RetrievalIndexEntry)
            .filter(
                RetrievalIndexEntry.index_version == index_version,
                RetrievalIndexEntry.is_current == True,  # noqa: E712
                RetrievalIndexEntry.id.in_(entry_ids),
            )
            .all()
        )

    def query_exact(
        self, db: Session, index_version: int, canonical_key: str, scope_id: str | None,
    ) -> list[RetrievalIndexEntry]:
        q = db.query(RetrievalIndexEntry).filter(
            RetrievalIndexEntry.index_version == index_version,
            RetrievalIndexEntry.is_current == True,  # noqa: E712
            RetrievalIndexEntry.is_searchable == True,  # noqa: E712
            RetrievalIndexEntry.source_type == "memory_record",
            RetrievalIndexEntry.canonical_key == canonical_key,
        )
        if scope_id is not None:
            q = q.filter(RetrievalIndexEntry.scope_id == scope_id)
        return q.all()

    # ── rebuild 分批游标 ──

    _SOURCE_TABLES: dict[str, type] = {
        "memory_record": MemoryRecord,
        "segment_summary": SegmentSummary,
        "epoch_checkpoint": EpochCheckpoint,
    }

    def next_source_batch(
        self, db: Session, source_type: str, last_id: str | None, limit: int,
    ) -> list[Any]:
        """按 id 升序取下一批准入源（rebuild 用）。返回 ORM 对象列表。"""
        model = self._SOURCE_TABLES.get(source_type)
        if model is None:
            return []
        q = db.query(model)
        if last_id is not None:
            q = q.filter(model.id > last_id)
        return q.order_by(model.id.asc()).limit(limit).all()
