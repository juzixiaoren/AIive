"""图谱重建与一致性修复：从 canonical 记忆记录恢复派生投影。

图谱不是真相源，因此"重建"永远是确定性的：读取 memory_records.graph_facts，
按与实时投影完全相同的 GraphProjectionService 逻辑重新落库，不调用 LLM、
不访问网络。可重复执行，重复执行结果一致（relation_key 幂等）。

覆盖的故障场景：
- 关系行丢失（索引损坏 / 手工清理）；
- Outbox 重试耗尽（投影任务终态失败）；
- 记忆版本变化后投影滞后；
- 维护中断留下半成品（部分关系已写、部分未写）；
- 旧版规则（不区分关系基数）误关的多值关系（repair_overclosed）。
"""
from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from aiive.db.models import GraphEntity, GraphRelation, MemoryRecord
from aiive.memory.graph_predicates import (
    conflicted_group_keys,
    is_single_valued,
    predicate_family,
)
from aiive.memory.graph_projection import GraphProjectionService, has_authoritative_supersede
from aiive.memory.graph_types import GraphConfig, GraphFact, normalize_entity_name
from aiive.memory.memory_types import LifecycleState, ValidityState

logger = logging.getLogger(__name__)

# 旧版规则写入的关闭原因：只凭"新出现不同客体"就关闭，可能误关多值关系
LEGACY_CONFLICT_REASON = "superseded_by_newer_fact"


class GraphRebuildService:
    """可重复执行的图谱重建与一致性核对。"""

    def __init__(self, db: Session, config: GraphConfig | None = None) -> None:
        self._db: Session = db
        self._config: GraphConfig = config or GraphConfig.from_settings()
        self._projection: GraphProjectionService = GraphProjectionService(db, self._config)

    def rebuild(self, *, batch_size: int = 200, repair: bool = True) -> dict[str, Any]:
        """重建全部投影，并清理孤儿行。

        repair=True 时先修复旧规则误关的关系（只恢复有确定依据的），再重新投影。
        """
        repair_report = self.repair_overclosed() if repair else None
        scanned = refreshed = skipped = 0
        offset = 0
        while True:
            records = (
                self._db.query(MemoryRecord)
                .order_by(MemoryRecord.id.asc())
                .offset(offset)
                .limit(batch_size)
                .all()
            )
            if not records:
                break
            offset += len(records)
            for record in records:
                scanned += 1
                if not record.graph_facts and not self._has_relations(record.id):
                    continue
                result = self._projection.refresh(record.id, record.record_version)
                if result.get("status") == "projected":
                    refreshed += 1
                else:
                    skipped += 1

        orphan_relations = self._purge_orphan_relations()
        orphan_entities = self._purge_orphan_entities()
        return {
            "scanned": scanned,
            "refreshed": refreshed,
            "skipped": skipped,
            "orphan_relations_removed": orphan_relations,
            "orphan_entities_removed": orphan_entities,
            "repair": repair_report,
            "verify": self.verify(),
        }

    def repair_overclosed(self, *, dry_run: bool = False) -> dict[str, Any]:
        """修复旧规则（不区分关系基数）误关的关系，只做有确定依据的恢复。

        判定顺序（全部以 memory_records 的权威状态为准）：
        1. 来源记忆已不存在 / 非 active → 不动，计入 skipped_source_inactive；
        2. 来源记忆 validity 已失效：若确有 superseded_by 指向的取代者 → 关闭是对的
           （kept_authoritative）；否则无法判定 → 保持现状并计入 kept_unresolved；
        3. 来源记忆仍 valid，但 canonical 列里已没有该事实 → 属于正常撤回
           （skipped_fact_removed）；
        4. 来源记忆仍 valid 且仍主张该事实 → 关闭是错的，恢复为 active。
           单值谓词也恢复：记忆层明确说这条事实仍成立，图谱无权替它裁决；
           恢复后与另一条并存，由读侧标为冲突（不是"确定事实"）。

        dry_run=True 只统计不写库，用于上线前评估影响面。
        """
        report: dict[str, Any] = {
            "scanned": 0,
            "reopened": 0,
            "reopened_multi_valued": 0,
            "reopened_single_valued": 0,
            "kept_authoritative": 0,
            "kept_unresolved": 0,
            "skipped_source_inactive": 0,
            "skipped_fact_removed": 0,
            "dry_run": dry_run,
        }
        rows = (
            self._db.query(GraphRelation)
            .filter(
                GraphRelation.closed_reason == LEGACY_CONFLICT_REASON,
                GraphRelation.status.in_(("closed", "retracted")),
            )
            .order_by(GraphRelation.recorded_at.asc(), GraphRelation.id.asc())
            .all()
        )
        for relation in rows:
            report["scanned"] += 1
            record = self._db.get(MemoryRecord, relation.source_memory_id)
            if record is None or (record.lifecycle_state or "") != LifecycleState.ACTIVE.value:
                report["skipped_source_inactive"] += 1
                continue
            if (record.validity_state or ValidityState.VALID.value) != ValidityState.VALID.value:
                if self._has_authoritative_supersede(record, relation):
                    report["kept_authoritative"] += 1
                else:
                    report["kept_unresolved"] += 1
                continue
            fact = self._matching_fact(record, relation)
            if fact is None:
                report["skipped_fact_removed"] += 1
                continue
            single = is_single_valued(relation.predicate)
            report["reopened"] += 1
            if single:
                report["reopened_single_valued"] += 1
            else:
                report["reopened_multi_valued"] += 1
            if dry_run:
                continue
            relation.status = "active"
            relation.closed_reason = None
            relation.superseded_by_id = None
            relation.valid_from = fact.valid_from
            relation.valid_to = fact.valid_to
            relation.updated_at = datetime.now(UTC)
        if not dry_run and report["reopened"]:
            self._db.flush()
        return report

    def verify(self) -> dict[str, Any]:
        """一致性核对：返回违规计数（正常应全为 0）。"""
        invalid_source = 0
        joined = (
            self._db.query(GraphRelation, MemoryRecord)
            .join(MemoryRecord, MemoryRecord.id == GraphRelation.source_memory_id)
            .filter(GraphRelation.status == "active")
            .all()
        )
        active_rows = [relation for relation, _record in joined]
        for _relation, record in joined:
            if (record.lifecycle_state or "") in (
                LifecycleState.FORGOTTEN.value,
                LifecycleState.CANDIDATE.value,
            ):
                invalid_source += 1
                continue
            if (record.validity_state or ValidityState.VALID.value) != ValidityState.VALID.value:
                invalid_source += 1
        orphan = (
            self._db.query(GraphRelation)
            .outerjoin(MemoryRecord, MemoryRecord.id == GraphRelation.source_memory_id)
            .filter(MemoryRecord.id.is_(None))
            .count()
        )
        missing_entity = (
            self._db.query(GraphRelation)
            .outerjoin(GraphEntity, GraphEntity.id == GraphRelation.subject_entity_id)
            .filter(GraphEntity.id.is_(None))
            .count()
        )
        conflicts = conflicted_group_keys([
            (
                relation.subject_entity_id,
                relation.predicate,
                relation.object_entity_id,
                relation.status,
                relation.scope_type,
                relation.scope_id,
            )
            for relation in active_rows
        ])
        suspect = sum(
            1
            for relation in self._db.query(GraphRelation)
            .filter(GraphRelation.closed_reason == LEGACY_CONFLICT_REASON)
            .all()
            if not is_single_valued(relation.predicate)
        )
        return {
            "active_relations": len(active_rows),
            "active_from_invalid_source": invalid_source,
            "orphan_relations": orphan,
            "relations_missing_subject_entity": missing_entity,
            # 单值谓词出现多个有效客体的分组数（正常的"待确认冲突"，不是错误）
            "conflicting_single_valued_groups": len(conflicts),
            # 仍被旧规则关闭、且谓词并非单值的关系数（修复后应为 0）
            "suspect_closed_relations": suspect,
        }

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _has_authoritative_supersede(self, record: MemoryRecord, relation: GraphRelation) -> bool:
        """关闭是否有记忆层依据：取代关系存在，且其来源记忆正是 superseded_by 指向者。"""
        if not relation.superseded_by_id:
            return False
        superseder = self._db.get(GraphRelation, relation.superseded_by_id)
        if superseder is None:
            return False
        return has_authoritative_supersede(record, superseder.source_memory_id)

    def _matching_fact(self, record: MemoryRecord, relation: GraphRelation) -> GraphFact | None:
        """在 canonical 列里找该关系对应的事实（用于确认"来源仍主张它"）。"""
        subject = self._db.get(GraphEntity, relation.subject_entity_id)
        object_entity = self._db.get(GraphEntity, relation.object_entity_id)
        if subject is None or object_entity is None:
            return None
        family = set(predicate_family(relation.predicate))
        for item in record.graph_facts or []:
            try:
                fact = GraphFact.model_validate(item)
            except (ValueError, TypeError):
                continue
            if not fact.is_complete() or fact.predicate not in family:
                continue
            if normalize_entity_name(fact.subject) != subject.normalized_name:
                continue
            if normalize_entity_name(fact.object) != object_entity.normalized_name:
                continue
            return fact
        return None

    def _has_relations(self, memory_id: str) -> bool:
        return (
            self._db.query(GraphRelation.id)
            .filter(GraphRelation.source_memory_id == memory_id)
            .first()
        ) is not None

    def _purge_orphan_relations(self) -> int:
        """删除来源记录已不存在的派生关系（不删除被取代的历史行）。"""
        rows = (
            self._db.query(GraphRelation)
            .outerjoin(MemoryRecord, MemoryRecord.id == GraphRelation.source_memory_id)
            .filter(MemoryRecord.id.is_(None))
            .all()
        )
        for relation in rows:
            self._db.delete(relation)
        if rows:
            self._db.flush()
        return len(rows)

    def _purge_orphan_entities(self) -> int:
        """删除没有任何关系引用的实体锚点。"""
        rows = (
            self._db.query(GraphEntity)
            .outerjoin(
                GraphRelation,
                or_(
                    GraphRelation.subject_entity_id == GraphEntity.id,
                    GraphRelation.object_entity_id == GraphEntity.id,
                ),
            )
            .filter(GraphRelation.id.is_(None))
            .all()
        )
        for entity in rows:
            self._db.delete(entity)
        if rows:
            self._db.flush()
        return len(rows)


def main(argv: list[str] | None = None) -> int:
    """命令行入口：python -m aiive.memory.graph_rebuild [--verify-only|--repair-only] [--dry-run]。"""
    parser = argparse.ArgumentParser(description="重建/核对/修复时序知识图谱投影")
    parser.add_argument("--verify-only", action="store_true", help="只做一致性核对，不重建")
    parser.add_argument("--repair-only", action="store_true", help="只修复旧规则误关的关系，不重建")
    parser.add_argument("--dry-run", action="store_true", help="配合 --repair-only：只统计不写库")
    args = parser.parse_args(argv)

    from aiive.db.base import SessionLocal

    db = SessionLocal()
    try:
        service = GraphRebuildService(db)
        if args.verify_only:
            report = service.verify()
        elif args.repair_only:
            report = service.repair_overclosed(dry_run=args.dry_run)
        else:
            report = service.rebuild()
        if args.verify_only or args.dry_run:
            db.rollback()
        else:
            db.commit()
    finally:
        db.close()
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
