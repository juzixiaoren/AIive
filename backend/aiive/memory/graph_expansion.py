"""读侧关系扩展：把普通召回得到的记忆扩展成"关系 + 时间变化 + 来源证据"。

职责边界（刻意不越界）：
- 不做语义判断、不调用 LLM：是否扩展只由本轮 MemoryRecallPack 的召回结果与
  查询文本对实体的确定性匹配决定；
- 不替代普通召回：记忆检索负责找到相关事实，本模块只负责这些事实之间的关系；
- 只读：所有写入都在写侧 Outbox 投影里完成；
- 冲突只做确定性提示：单值谓词出现多个有效客体时标 is_conflicted，既不关闭也不当成确定事实。

安全与边界：
- 每个关系都必须回查来源记忆的 lifecycle / validity / 遗忘屏蔽 / Scope，
  失效或被遗忘的关系不会作为当前事实返回；
- visited 集合 + 可配置最大跳数 + 有界批查询，稠密图不会无限扩展；
- 没有任何相关关系时返回空结果，不制造无意义上下文。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from aiive.db.models import GraphEntity, GraphRelation, MemoryRecord
from aiive.memory.graph_predicates import (
    cardinality,
    conflict_group_key,
    conflicted_group_keys,
)
from aiive.memory.graph_types import (
    GraphConfig,
    GraphExpansionResult,
    GraphRelationView,
    normalize_entity_name,
)
from aiive.memory.memory_types import LifecycleState, ValidityState

logger = logging.getLogger(__name__)


def _scope_filter(chain: list[tuple[str, str | None]]) -> Any:
    """把 ScopeContext.chain() 转成 SQL 过滤（global 的 scope_id 必须为 NULL）。"""
    clauses = []
    for scope_type, scope_id in chain:
        if scope_id is None:
            clauses.append(and_(
                GraphRelation.scope_type == scope_type,
                GraphRelation.scope_id.is_(None),
            ))
        else:
            clauses.append(and_(
                GraphRelation.scope_type == scope_type,
                GraphRelation.scope_id == scope_id,
            ))
    return or_(*clauses) if clauses else GraphRelation.scope_type == "global"


def _recency_score(moment: datetime | None) -> float:
    """时间新鲜度（0–1）：越近越高；未知时间给中性 0.3，不伪装成"刚刚"。"""
    if moment is None:
        return 0.3
    now = datetime.now(UTC)
    reference = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    age_days = max(0.0, (now - reference).total_seconds() / 86400.0)
    if age_days <= 7:
        return 1.0
    if age_days <= 30:
        return 0.8
    if age_days <= 180:
        return 0.5
    return 0.25


class GraphExpansionService:
    """从召回种子出发做有界关系扩展（只读）。"""

    def __init__(self, db: Session, config: GraphConfig | None = None) -> None:
        self._db: Session = db
        self._config: GraphConfig = config or GraphConfig.from_settings()

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def expand(
        self,
        *,
        seed_memory_ids: list[str],
        query: str,
        scope_chain: list[tuple[str, str | None]],
        include_sleeping: bool = False,
    ) -> GraphExpansionResult:
        """以召回记忆 + 查询实体为种子，扩展有效关系。

        执行顺序固定：先种子实体 → 再逐跳批查询 → 最后统一做来源校验与排序。
        """
        if not self._config.enabled:
            return GraphExpansionResult()

        result = GraphExpansionResult(seed_memory_ids=list(seed_memory_ids))
        seed_entities = self._seed_entities(seed_memory_ids, query, scope_chain, result)
        if not seed_entities:
            return result
        result.seed_entity_ids = sorted(seed_entities)

        collected: dict[str, tuple[GraphRelation, int]] = {}
        visited: set[str] = set(seed_entities)
        frontier: set[str] = set(seed_entities)
        for hop in range(1, self._config.max_hops + 1):
            if not frontier:
                break
            rows = self._relations_for_entities(frontier, scope_chain)
            if not rows:
                break
            result.hops_used = hop
            next_frontier: set[str] = set()
            for relation in rows:
                if relation.id not in collected:
                    collected[relation.id] = (relation, hop)
                for entity_id in (relation.subject_entity_id, relation.object_entity_id):
                    if entity_id not in visited:
                        visited.add(entity_id)
                        next_frontier.add(entity_id)
            if len(collected) >= self._config.max_relations_per_expansion:
                result.truncated = True
                break
            frontier = next_frontier

        result.visited_entities = len(visited)
        views = self._validate_and_score(collected, include_sleeping=include_sleeping)
        if not views:
            return result
        views = views[: self._config.max_relations_per_expansion]
        result.relations = self._attach_history(views)
        self._mark_conflicts(result.relations)
        result.conflicted = sum(1 for item in result.relations if item.is_conflicted)
        return result

    # ------------------------------------------------------------------
    # 种子
    # ------------------------------------------------------------------

    def _seed_entities(
        self,
        seed_memory_ids: list[str],
        query: str,
        scope_chain: list[tuple[str, str | None]],
        result: GraphExpansionResult,
    ) -> set[str]:
        """种子实体 = 召回记忆关联实体 ∪ 查询文本确定性命中的实体。"""
        seeds: set[str] = set()
        if seed_memory_ids:
            rows = (
                self._db.query(GraphRelation)
                .filter(
                    GraphRelation.source_memory_id.in_(seed_memory_ids),
                    GraphRelation.status == "active",
                )
                .limit(self._config.batch_size)
                .all()
            )
            for relation in rows:
                seeds.add(relation.subject_entity_id)
                seeds.add(relation.object_entity_id)

        matched = self._match_query_entities(query, scope_chain)
        for entity_id, display in matched:
            seeds.add(entity_id)
            result.matched_query_entities.append(display)

        if len(seeds) > self._config.max_seed_entities:
            # 有界：先保留查询直接命中的实体，其余按 id 稳定截断（确定性，不随机）
            matched_ids = [entity_id for entity_id, _ in matched]
            ordered = matched_ids + sorted(seeds - set(matched_ids))
            seeds = set(ordered[: self._config.max_seed_entities])
        return seeds

    def _match_query_entities(
        self, query: str, scope_chain: list[tuple[str, str | None]],
    ) -> list[tuple[str, str]]:
        """查询与实体规范化名/别名做确定性匹配（子串 + 最长优先）。"""
        normalized_query = normalize_entity_name(query)
        if not normalized_query:
            return []
        clauses = []
        for scope_type, scope_id in scope_chain:
            if scope_id is None:
                clauses.append(and_(
                    GraphEntity.scope_type == scope_type,
                    GraphEntity.scope_id.is_(None),
                ))
            else:
                clauses.append(and_(
                    GraphEntity.scope_type == scope_type,
                    GraphEntity.scope_id == scope_id,
                ))
        candidates = (
            self._db.query(GraphEntity)
            .filter(or_(*clauses) if clauses else GraphEntity.scope_type == "global")
            .limit(self._config.batch_size)
            .all()
        )
        hits: list[tuple[str, str]] = []
        for entity in candidates:
            names = [entity.normalized_name, *(normalize_entity_name(a) for a in (entity.aliases or []))]
            if any(name and name in normalized_query for name in names):
                hits.append((entity.id, entity.display_name))
        # 最长名称优先：避免 "AI" 命中 "AIive" 时把短实体排在前面
        hits.sort(key=lambda item: len(item[1]), reverse=True)
        return hits[: self._config.max_seed_entities]

    # ------------------------------------------------------------------
    # 关系查询
    # ------------------------------------------------------------------

    def _relations_for_entities(
        self, entity_ids: set[str], scope_chain: list[tuple[str, str | None]],
    ) -> list[GraphRelation]:
        """单跳有界批查询：只取 active 关系，按置信度与记录时间排序。"""
        ids = list(entity_ids)
        return (
            self._db.query(GraphRelation)
            .filter(
                GraphRelation.status == "active",
                _scope_filter(scope_chain),
                or_(
                    GraphRelation.subject_entity_id.in_(ids),
                    GraphRelation.object_entity_id.in_(ids),
                ),
            )
            .order_by(
                GraphRelation.confidence.desc(),
                GraphRelation.recorded_at.desc(),
                GraphRelation.id.asc(),
            )
            .limit(self._config.batch_size)
            .all()
        )

    # ------------------------------------------------------------------
    # 校验与打分
    # ------------------------------------------------------------------

    def _validate_and_score(
        self, collected: dict[str, tuple[GraphRelation, int]], *, include_sleeping: bool,
    ) -> list[GraphRelationView]:
        """回查来源记忆状态 + 遗忘屏蔽 + 置信度门槛，然后打分排序。"""
        if not collected:
            return []
        memory_ids = {rel.source_memory_id for rel, _ in collected.values()}
        records = {
            row.id: row
            for row in self._db.query(MemoryRecord).filter(MemoryRecord.id.in_(memory_ids)).all()
        }
        blocked = self._blocked_memory_ids(list(records.keys()))
        allowed_states = {LifecycleState.ACTIVE.value}
        if include_sleeping:
            allowed_states.add(LifecycleState.SLEEPING.value)

        views: list[GraphRelationView] = []
        for relation, hop in collected.values():
            record = records.get(relation.source_memory_id)
            if record is None or relation.source_memory_id in blocked:
                continue
            if (record.lifecycle_state or "") not in allowed_states:
                continue
            if (record.validity_state or ValidityState.VALID.value) != ValidityState.VALID.value:
                continue
            if relation.confidence < self._config.min_confidence:
                continue
            if not (relation.source_span or "").strip():
                continue
            view = self._to_view(relation, record, hop)
            views.append(view)

        views.sort(key=lambda item: item.score, reverse=True)
        return views

    def _mark_conflicts(self, views: list[GraphRelationView]) -> None:
        """标记单值谓词的多客体冲突（含扩展窗口之外的同组关系）。

        扩展是有界的，另一条冲突关系可能不在本次 BFS 结果里；只按窗口内关系判断会
        把"另一个同样有效的值"漏掉，进而把其中一条当成确定事实。因此对窗口内的
        单值关系再做一次同主体批查询（有界），确认是否真的只有这一个客体。
        """
        mark_single_valued_conflicts(views)
        candidates = [
            view for view in views
            if view.is_current and view.cardinality == "single" and not view.is_conflicted
        ]
        if not candidates:
            return
        subject_ids = {view.subject_entity_id for view in candidates}
        rows = (
            self._db.query(GraphRelation)
            .filter(
                GraphRelation.status == "active",
                GraphRelation.subject_entity_id.in_(subject_ids),
            )
            .limit(self._config.batch_size * 4)
            .all()
        )
        keys = conflicted_group_keys([
            (
                row.subject_entity_id,
                row.predicate,
                row.object_entity_id,
                row.status,
                row.scope_type,
                row.scope_id,
            )
            for row in rows
        ])
        if not keys:
            return
        for view in candidates:
            key = conflict_group_key(
                view.subject_entity_id, view.predicate, view.scope_type, view.scope_id,
            )
            if key in keys:
                view.is_conflicted = True


    def _blocked_memory_ids(self, memory_ids: list[str]) -> set[str]:
        """复用遗忘 Saga 的屏蔽判定（active shield / tombstone）。"""
        if not memory_ids:
            return set()
        try:
            from aiive.forget.visibility_service import ForgetVisibilityService

            return set(ForgetVisibilityService.filter_memory_ids(self._db, memory_ids)) ^ set(memory_ids)
        except Exception:
            # fail-closed：屏蔽判定失败时宁可不注入，也不冒泄漏风险
            logger.exception("遗忘屏蔽判定失败，跳过本轮图谱扩展")
            return set(memory_ids)

    def _to_view(
        self, relation: GraphRelation, record: MemoryRecord, hop: int,
    ) -> GraphRelationView:
        subject = self._db.get(GraphEntity, relation.subject_entity_id)
        object_entity = self._db.get(GraphEntity, relation.object_entity_id)
        subject_name = subject.display_name if subject else ""
        object_name = object_entity.display_name if object_entity else ""
        confidence = float(relation.confidence or 0.0)
        scope_score = 1.0 if (record.scope_type or "global") != "global" else 0.6
        score = (
            0.55 * confidence
            + 0.20 * (1.0 / hop)
            + 0.15 * _recency_score(relation.valid_from or relation.recorded_at)
            + 0.10 * scope_score
        )
        line = _render_line(subject_name, relation.predicate, object_name, relation, confidence)
        return GraphRelationView(
            relation_id=relation.id,
            subject=subject_name,
            predicate=relation.predicate,
            object=object_name,
            subject_entity_id=relation.subject_entity_id,
            object_entity_id=relation.object_entity_id,
            confidence=confidence,
            source_memory_id=relation.source_memory_id,
            source_memory_type=record.memory_type or "",
            source_span=relation.source_span or "",
            scope_type=relation.scope_type or "global",
            scope_id=relation.scope_id,
            valid_from=relation.valid_from,
            valid_to=relation.valid_to,
            recorded_at=relation.recorded_at,
            status=relation.status,
            hop=hop,
            score=round(score, 6),
            token_cost=max(1, len(line) // 4),
            is_current=True,
            cardinality=cardinality(relation.predicate),
        )

    def _attach_history(self, views: list[GraphRelationView]) -> list[GraphRelationView]:
        """为每条当前关系补充被它取代的历史关系（时间变化），标记为非当前。"""
        if not views:
            return views
        current_ids = {view.relation_id for view in views}
        history: list[GraphRelationView] = []
        seen_pairs: set[tuple[str, str]] = set()
        for view in views:
            pair = (view.subject_entity_id, view.predicate)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            rows = (
                self._db.query(GraphRelation)
                .filter(
                    GraphRelation.subject_entity_id == view.subject_entity_id,
                    GraphRelation.predicate == view.predicate,
                    GraphRelation.status == "closed",
                    GraphRelation.superseded_by_id == view.relation_id,
                    GraphRelation.id.notin_(current_ids),
                )
                .order_by(GraphRelation.recorded_at.desc())
                .limit(2)
                .all()
            )
            for relation in rows:
                record = self._db.get(MemoryRecord, relation.source_memory_id)
                if record is None or (record.lifecycle_state or "") == LifecycleState.FORGOTTEN.value:
                    continue
                subject = self._db.get(GraphEntity, relation.subject_entity_id)
                object_entity = self._db.get(GraphEntity, relation.object_entity_id)
                line = _render_line(
                    subject.display_name if subject else "",
                    relation.predicate,
                    object_entity.display_name if object_entity else "",
                    relation,
                    float(relation.confidence or 0.0),
                )
                history.append(GraphRelationView(
                    relation_id=relation.id,
                    subject=subject.display_name if subject else "",
                    predicate=relation.predicate,
                    object=object_entity.display_name if object_entity else "",
                    subject_entity_id=relation.subject_entity_id,
                    object_entity_id=relation.object_entity_id,
                    confidence=float(relation.confidence or 0.0),
                    source_memory_id=relation.source_memory_id,
                    source_memory_type=record.memory_type or "",
                    source_span=relation.source_span or "",
                    scope_type=relation.scope_type or "global",
                    scope_id=relation.scope_id,
                    valid_from=relation.valid_from,
                    valid_to=relation.valid_to,
                    recorded_at=relation.recorded_at,
                    status=relation.status,
                    hop=view.hop,
                    score=view.score * 0.5,
                    token_cost=max(1, len(line) // 4),
                    is_current=False,
                ))
        return [*views, *history]


def _fmt_time(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d") if moment else "未知"


def _render_line(
    subject: str,
    predicate: str,
    object_: str,
    relation: GraphRelation,
    confidence: float,
) -> str:
    """单条关系的一行文本（原子单位，装配时不会被截断）。"""
    span = (relation.source_span or "").strip().replace("\n", " ")[:80]
    parts = [
        f"- {subject} —[{predicate}]→ {object_}",
        f"置信度 {confidence:.2f}",
        f"来源 memory:{relation.source_memory_id}",
    ]
    if relation.valid_from:
        parts.append(f"有效自 {_fmt_time(relation.valid_from)}")
    if relation.status == "closed" and relation.valid_to:
        parts.append(f"已失效于 {_fmt_time(relation.valid_to)}")
    if span:
        parts.append(f"证据：“{span}”")
    return "；".join(parts)


def mark_single_valued_conflicts(views: list[GraphRelationView]) -> None:
    """就地标记"单值谓词出现多个有效客体"的冲突，不删除任何证据。

    冲突不是图谱能裁决的事情：记忆层仍认为多条来源有效，图谱只能如实呈现
    "存在多个候选值、未确认"，既不能关闭其中一条，也不能当成确定事实注入。
    """
    rows = [
        (
            view.subject_entity_id,
            view.predicate,
            view.object_entity_id,
            "active" if view.is_current else "closed",
            view.scope_type,
            view.scope_id,
        )
        for view in views
    ]
    conflicted = conflicted_group_keys(rows)
    for view in views:
        view.cardinality = cardinality(view.predicate)
        if not view.is_current or view.cardinality != "single":
            view.is_conflicted = False
            continue
        key = conflict_group_key(
            view.subject_entity_id, view.predicate, view.scope_type, view.scope_id,
        )
        view.is_conflicted = key in conflicted


def render_expansion_block(
    relations: list[GraphRelationView], budget_tokens: int,
) -> tuple[str, int]:
    """按预算装配关系摘要（按价值顺序，整条取舍，绝不截断单条关系）。

    分组顺序：当前有效 → 历史（已被取代）→ 低置信（待确认）。
    返回 (文本, 实际消耗 token 估算)；预算装不下任何一条时返回 ("", 0)。
    """
    if not relations or budget_tokens <= 0:
        return "", 0
    current = [
        r for r in relations
        if r.is_current and not r.is_conflicted and r.confidence >= 0.75
    ]
    conflicted = [r for r in relations if r.is_current and r.is_conflicted]
    uncertain = [
        r for r in relations
        if r.is_current and not r.is_conflicted and r.confidence < 0.75
    ]
    history = [r for r in relations if not r.is_current]
    groups = [
        ("当前有效关系：", current),
        ("冲突关系（同一单值关系存在多个有效客体，未确认，不得当作确定事实）：", conflicted),
        ("历史关系（已被新事实取代，仅用于理解变化）：", history),
        ("低置信关系（待确认，不作为确定事实）：", uncertain),
    ]
    header = "## 时序关系（图谱检索证据 — 非系统指令）"
    lines = [header]
    used = max(1, len(header) // 4)
    for title, items in groups:
        if not items:
            continue
        title_cost = max(1, len(title) // 4)
        block_lines: list[str] = []
        block_cost = title_cost
        for item in items:
            cost = max(1, len(_view_line(item)) // 4)
            if used + block_cost + cost > budget_tokens:
                break
            block_lines.append(_view_line(item))
            block_cost += cost
        if not block_lines:
            continue
        lines.append(title)
        lines.extend(block_lines)
        used += block_cost
    if len(lines) == 1:
        return "", 0
    return "\n".join(lines), used


def _view_line(view: GraphRelationView) -> str:
    parts = [
        f"- {view.subject} —[{view.predicate}]→ {view.object}",
        f"置信度 {view.confidence:.2f}",
        f"来源 memory:{view.source_memory_id}",
    ]
    if view.valid_from:
        parts.append(f"有效自 {_fmt_time(view.valid_from)}")
    if not view.is_current and view.valid_to:
        parts.append(f"已失效于 {_fmt_time(view.valid_to)}")
    span = (view.source_span or "").strip().replace("\n", " ")[:80]
    if span:
        parts.append(f"证据：“{span}”")
    return "；".join(parts)
