"""图谱投影：把 canonical 记忆记录里的关系事实同步到 graph_* 派生表。

写侧唯一入口是 Outbox 的 memory_graph_refresh 任务（由
MemoryMutationExecutor.enqueue_projection 入队），因此创建、加强、取代、
合并、晋升、睡眠、唤醒、归档全部复用同一条投影路径；本模块不参与写入决策，
也不写入 memory_records。

幂等与一致性：
- relation_key = 来源记忆 + 三元组 + 事实起始时间，重复投影只更新版本与置信度；
- 同一记忆的新版本若不再包含某条事实，只撤回**它自己**的旧关系（status=retracted），
  不会误删其他记忆支持的关系；
- 只有明确声明为单值的谓词（graph_predicates）才可能在同主体 + 同谓词族 +
  同 Scope 出现不同客体时结束旧关系；并且必须有记忆层的权威取代结果
  （旧记录 validity 失效且 superseded_by 指向本条记忆），不凭图谱里新出现
  一条关系就判定旧事实失效；
- 多值 / 未知谓词永远不因新事实出现而关闭旧关系，多个客体可以并存；
- 记录被取代/失效/遗忘时，其关系全部结束（遗忘时直接删除，不留内容）。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import or_
from sqlalchemy.orm import Session

from aiive.db.models import GraphEntity, GraphRelation, MemoryRecord
from aiive.memory.graph_predicates import is_single_valued, predicate_family
from aiive.memory.graph_types import (
    GraphConfig,
    GraphFact,
    display_entity_name,
    normalize_entity_name,
)
from aiive.memory.memory_types import LifecycleState, ValidityState

logger = logging.getLogger(__name__)

_INVALID_VALIDITY = {
    ValidityState.SUPERSEDED.value,
    ValidityState.CONTRADICTED.value,
    ValidityState.EXPIRED.value,
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite 会把 DateTime(timezone=True) 读成 naive，比较前统一补齐时区。"""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def has_authoritative_supersede(old_record: MemoryRecord, new_memory_id: str) -> bool:
    """旧记忆是否已被**记忆层**权威地取代为 new_memory_id。

    图谱只跟随 ConflictResolver / MemoryWriteService 的结果：旧记录 validity 已失效，
    且 superseded_by 明确指向新记忆。缺任一条都视为"图谱没有资格判定旧事实失效"。
    """
    if (old_record.validity_state or "") not in _INVALID_VALIDITY:
        return False
    return bool(old_record.superseded_by) and old_record.superseded_by == new_memory_id


class GraphProjectionService:
    """按来源记忆刷新图谱投影（Outbox handler 与重建任务共用）。"""

    def __init__(self, db: Session, config: GraphConfig | None = None) -> None:
        self._db: Session = db
        self._config: GraphConfig = config or GraphConfig.from_settings()

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    def refresh(self, memory_id: str, record_version: int) -> dict[str, Any]:
        """把一条记忆当前版本的关系事实投影到图谱（幂等）。

        Returns:
            统计字典：status + created/updated/closed/purged 计数。
        """
        record = self._db.get(MemoryRecord, memory_id)
        if record is None:
            # 来源记录不存在：派生行没有意义，直接清理，避免孤儿关系。
            purged = self.purge_for_memory(memory_id)
            return {"status": "missing_source", "purged": purged}

        if record_version > 0 and record.record_version != record_version:
            # 旧版本任务迟到：新版本任务会写正确内容，这里只做无害返回。
            return {
                "status": "stale_skipped",
                "record_version": record.record_version,
                "job_version": record_version,
            }

        lifecycle = record.lifecycle_state or ""
        validity = record.validity_state or ""

        if lifecycle == LifecycleState.FORGOTTEN.value:
            purged = self.purge_for_memory(memory_id)
            return {"status": "forgotten_purged", "purged": purged}

        if lifecycle == LifecycleState.CANDIDATE.value:
            # 只有被正式接受的记忆才能成为事实来源。
            closed = self._close_own_relations(
                memory_id, keep_keys=set(), reason="not_accepted", status="retracted",
            )
            return {"status": "candidate_skipped", "closed": closed}

        if validity in _INVALID_VALIDITY:
            closed = self._close_own_relations(
                memory_id, keep_keys=set(), reason=f"record_{validity}",
            )
            return {"status": "record_invalid_closed", "closed": closed}

        facts = self._load_facts(record)
        if not facts:
            closed = self._close_own_relations(
                memory_id, keep_keys=set(), reason="no_facts", status="retracted",
            )
            return {"status": "no_facts", "closed": closed}

        created = updated = 0
        kept_keys: set[str] = set()
        for fact in facts:
            relation, was_created = self._upsert_relation(record, fact)
            kept_keys.add(relation.relation_key)
            if was_created:
                created += 1
            else:
                updated += 1

        closed = self._close_own_relations(
            memory_id, keep_keys=kept_keys, reason="fact_removed", status="retracted",
        )
        return {
            "status": "projected",
            "created": created,
            "updated": updated,
            "closed": closed,
            "facts": len(facts),
        }

    def purge_for_memory(self, memory_id: str) -> int:
        """删除某条记忆的全部关系行（遗忘/来源丢失）。

        遗忘是隐私承诺：关系行连同实体锚点一起清空，不保留可被重新召回的历史。
        """
        relations = (
            self._db.query(GraphRelation)
            .filter(GraphRelation.source_memory_id == memory_id)
            .all()
        )
        entity_ids = {r.subject_entity_id for r in relations} | {
            r.object_entity_id for r in relations
        }
        for relation in relations:
            self._db.delete(relation)
        self._db.flush()
        self._delete_orphan_entities(entity_ids)
        return len(relations)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _load_facts(self, record: MemoryRecord) -> list[GraphFact]:
        """从 canonical 列解析事实；无证据（空 span）或低置信的条目不入图。"""
        raw = record.graph_facts or []
        facts: list[GraphFact] = []
        for item in raw:
            try:
                fact = GraphFact.model_validate(item)
            except (ValueError, TypeError, ValidationError):
                logger.debug("跳过无法解析的关系事实: memory_id=%s", record.id, exc_info=True)
                continue
            if not fact.is_complete():
                continue
            if not (fact.source_span or "").strip():
                # 没有原文依据的关系不进入图谱，避免无源事实被当作确定事实。
                continue
            if fact.confidence < self._config.min_confidence:
                # 低置信关系保留在 canonical 列里（可追溯），但不投影为可用事实。
                continue
            facts.append(fact)
        return facts

    def _upsert_relation(
        self, record: MemoryRecord, fact: GraphFact,
    ) -> tuple[GraphRelation, bool]:
        subject = self._resolve_entity(record, fact.subject, fact.subject_entity_id)
        object_entity = self._resolve_entity(record, fact.object, fact.object_entity_id)
        key = fact.normalized_key(record.id)
        existing = (
            self._db.query(GraphRelation)
            .filter(GraphRelation.relation_key == key)
            .one_or_none()
        )
        now = _utcnow()
        if existing is not None:
            existing.subject_entity_id = subject.id
            existing.object_entity_id = object_entity.id
            existing.predicate = fact.predicate
            existing.confidence = fact.confidence
            existing.source_span = fact.source_span or None
            existing.source_record_version = record.record_version
            existing.valid_from = fact.valid_from
            existing.valid_to = fact.valid_to
            existing.updated_at = now
            if existing.status != "active":
                # 同一事实重新出现（例如记录被唤醒）：重新打开并清掉关闭原因。
                existing.status = "active"
                existing.closed_reason = None
                existing.superseded_by_id = None
            self._db.flush()
            self._close_conflicts(record, fact, subject, object_entity, existing)
            return existing, False

        relation = GraphRelation(
            relation_key=key,
            subject_entity_id=subject.id,
            object_entity_id=object_entity.id,
            predicate=fact.predicate,
            source_memory_id=record.id,
            source_record_version=record.record_version,
            scope_type=record.scope_type or "global",
            scope_id=record.scope_id,
            confidence=fact.confidence,
            source_span=fact.source_span or None,
            valid_from=fact.valid_from,
            valid_to=fact.valid_to,
            recorded_at=now,
            status="active",
        )
        self._db.add(relation)
        self._db.flush()
        self._close_conflicts(record, fact, subject, object_entity, relation)
        return relation, True

    def _resolve_entity(
        self,
        record: MemoryRecord,
        name: str,
        bound_entity_id: str | None,
    ) -> GraphEntity:
        """优先复用裁决阶段确认过的实体锚点，不可用时退回按名称解析。

        绑定只是提示，投影阶段必须重新校验：实体可能已被遗忘级联删除，
        也可能属于别的 Scope。任一不满足就放弃绑定，走原有的按名称新建/复用
        路径——绝不因为"曾经绑定过"就把事实挂到无效或越界的锚点上。
        """
        if bound_entity_id:
            entity = self._db.get(GraphEntity, bound_entity_id)
            if entity is not None and self._scope_matches(record, entity):
                # 把新写法记成别名，后续同义名称可以直接按别名确定性复用。
                self._remember_alias(entity, name)
                return entity
            logger.debug(
                "实体绑定失效，退回按名称解析: memory_id=%s name=%s bound=%s",
                record.id, name, bound_entity_id,
            )
        return self._upsert_entity(record, name)

    @staticmethod
    def _scope_matches(record: MemoryRecord, entity: GraphEntity) -> bool:
        if (entity.scope_type or "global") != (record.scope_type or "global"):
            return False
        return (entity.scope_id or None) == (record.scope_id or None)

    @staticmethod
    def _remember_alias(entity: GraphEntity, name: str) -> None:
        display = display_entity_name(name)
        if not display or display == entity.display_name:
            return
        aliases = list(entity.aliases or [])
        if display in aliases:
            return
        aliases.append(display)
        entity.aliases = aliases[:12]
        entity.updated_at = _utcnow()

    def _upsert_entity(self, record: MemoryRecord, name: str) -> GraphEntity:
        normalized = normalize_entity_name(name)
        scope_type = record.scope_type or "global"
        scope_id = record.scope_id
        entity = (
            self._db.query(GraphEntity)
            .filter(
                GraphEntity.normalized_name == normalized,
                GraphEntity.scope_type == scope_type,
                GraphEntity.scope_id.is_(None) if scope_id is None
                else GraphEntity.scope_id == scope_id,
            )
            .one_or_none()
        )
        display = display_entity_name(name)
        if entity is None:
            entity = GraphEntity(
                normalized_name=normalized,
                display_name=display,
                entity_type="unknown",
                scope_type=scope_type,
                scope_id=scope_id,
            )
            self._db.add(entity)
            self._db.flush()
            return entity
        aliases = list(entity.aliases or [])
        if display and display != entity.display_name and display not in aliases:
            aliases.append(display)
            entity.aliases = aliases[:12]
            entity.updated_at = _utcnow()
        return entity

    def _close_conflicts(
        self,
        record: MemoryRecord,
        fact: GraphFact,
        subject: GraphEntity,
        object_entity: GraphEntity,
        current: GraphRelation,
    ) -> int:
        """结束被本条记忆权威取代的旧关系（单值谓词才允许）。

        两道闸门缺一不可：
        1. 谓词被明确声明为单值（graph_predicates）；多值 / 未知谓词允许多客体并存，
           即使客体不同也保持 active；
        2. 旧关系的来源记忆已被记忆层权威取代为本条记忆（validity 失效且
           superseded_by 指向本条）。不凭图谱里新出现一条关系就判定旧事实失效。

        不满足条件的旧关系保持 active，由读侧标记为冲突并交回记忆层裁决。
        返回本次关闭的关系条数。
        """
        if not is_single_valued(fact.predicate):
            return 0
        family = predicate_family(fact.predicate)
        if not family:
            return 0
        candidates = (
            self._db.query(GraphRelation)
            .filter(
                GraphRelation.subject_entity_id == subject.id,
                GraphRelation.predicate.in_(family),
                GraphRelation.status == "active",
                GraphRelation.id != current.id,
                GraphRelation.object_entity_id != object_entity.id,
                GraphRelation.scope_type == (record.scope_type or "global"),
                or_(
                    GraphRelation.scope_id.is_(None),
                    GraphRelation.scope_id == record.scope_id,
                ),
            )
            .all()
        )
        closed_at = fact.valid_from or _utcnow()
        current_recorded = _as_utc(current.recorded_at)
        closed = 0
        for old in candidates:
            if old.source_memory_id == current.source_memory_id:
                # 同一条记忆内部的并存：不在这里关闭，交给读侧冲突标记。
                continue
            old_record = self._db.get(MemoryRecord, old.source_memory_id)
            if old_record is None:
                continue
            if not has_authoritative_supersede(old_record, record.id):
                continue
            old_recorded = _as_utc(old.recorded_at)
            if old_recorded and current_recorded and old_recorded > current_recorded:
                continue
            old.status = "closed"
            old.closed_reason = "superseded_by_newer_fact"
            old.superseded_by_id = current.id
            old.valid_to = closed_at
            old.updated_at = _utcnow()
            closed += 1
        return closed

    def _close_own_relations(
        self, memory_id: str, *, keep_keys: set[str], reason: str, status: str = "closed",
    ) -> int:
        """结束该记忆名下不再成立的关系（保留行以便追溯）。

        status 区分两种语义：
        - closed：有效期结束（来源记忆被取代 / 失效 / 过期），可写 valid_to；
        - retracted：来源记忆不再主张该事实（版本删除、未接受、无事实），
          不伪造事实结束时间，只标记撤回。
        """
        query = self._db.query(GraphRelation).filter(
            GraphRelation.source_memory_id == memory_id,
            GraphRelation.status == "active",
        )
        closed = 0
        now = _utcnow()
        for relation in query.all():
            if relation.relation_key in keep_keys:
                continue
            relation.status = status
            relation.closed_reason = reason
            if status == "closed":
                relation.valid_to = relation.valid_to or now
            relation.updated_at = now
            closed += 1
        return closed

    def _delete_orphan_entities(self, entity_ids: set[str]) -> None:
        """删除没有任何关系引用的实体锚点。"""
        for entity_id in entity_ids:
            still_used = (
                self._db.query(GraphRelation.id)
                .filter(or_(
                    GraphRelation.subject_entity_id == entity_id,
                    GraphRelation.object_entity_id == entity_id,
                ))
                .first()
            )
            if still_used is None:
                entity = self._db.get(GraphEntity, entity_id)
                if entity is not None:
                    self._db.delete(entity)
        self._db.flush()
