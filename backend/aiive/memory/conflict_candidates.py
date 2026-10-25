"""冲突候选发现：把"新提案可能撞上的旧记忆"找全，而不是只信一条路。

三路来源，全部复用既有实现，不另建检索引擎：

1. 精确键（canonical_key + Scope）：与 MemoryWriteService 写入前查找同一套
   判定口径（active / sleeping + validity=valid），保证"同键不同内容"这类
   确定性冲突一定被看到；
2. 统一检索（UnifiedRetriever → AutomaticRecallEngine）：按当前部署的关键词 /
   向量 / 混合配置做语义召回。检索未命中**不**等于没有冲突，因此它只是三路之一；
3. 时序知识图谱（graph_relations）：用新提案自带的 graph_facts 反查"同主体 +
   同谓词族 + 同 Scope"的现有有效关系，把关系另一端的来源记忆补进来。这条路径
   能发现"键不同但说的是同一件事"的冲突（例如 user.lives_in 与
   user.profile.city 都描述居住地）。

合并规则：按 memory_id 去重、保留最高检索分、合并 sources；随后统一回源校验
（存在 / lifecycle / validity / 遗忘屏蔽 / Scope 可见），并剔除与新提案内容
完全相同（content_hash 相同，属于 reinforce 而非冲突）的记录。

图谱关系只作为**判断证据**随候选返回，本模块不据此关闭或改写任何关系。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from aiive.db.models import GraphEntity, GraphRelation, MemoryRecord
from aiive.memory.graph_predicates import cardinality, predicate_family
from aiive.memory.graph_types import GraphFact, normalize_entity_name
from aiive.memory.memory_types import LifecycleState, ValidityState

logger = logging.getLogger(__name__)

# 候选来源标签（写入案例证据，便于审计"这条冲突是怎么发现的"）
SOURCE_EXACT = "exact_key"
SOURCE_RETRIEVAL = "retrieval"
SOURCE_GRAPH = "graph"

# 参与冲突判定的旧记忆状态：候选态不是事实，不构成冲突对象
_CONFLICT_STATES = (LifecycleState.ACTIVE.value, LifecycleState.SLEEPING.value)

# 实体别名兜底匹配的扫描上限（有界查询，避免稠密图全表扫描）
_ALIAS_SCAN_LIMIT = 300


@dataclass
class ConflictCandidate:
    """一条可能与新提案冲突的既有记忆（已通过状态与可见性校验）。"""

    memory_id: str
    content: str
    canonical_key: str
    memory_type: str
    scope_type: str
    scope_id: str | None
    record_version: int
    confidence: float
    trust_level: str
    sources: list[str] = field(default_factory=list)
    retrieval_score: float = 0.0
    graph_relations: list[dict[str, Any]] = field(default_factory=list)

    def to_evidence(self) -> dict[str, Any]:
        """转成可持久化的证据快照（不含敏感原文之外的额外推断）。"""
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "canonical_key": self.canonical_key,
            "memory_type": self.memory_type,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "record_version": self.record_version,
            "confidence": round(self.confidence, 4),
            "trust_level": self.trust_level,
            "sources": list(self.sources),
            "retrieval_score": round(self.retrieval_score, 4),
        }


@dataclass
class CandidateDiscovery:
    """一次候选发现的结果。"""

    candidates: list[ConflictCandidate] = field(default_factory=list)
    graph_evidence: list[dict[str, Any]] = field(default_factory=list)
    exact_conflict: bool = False
    notes: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.candidates and not self.graph_evidence


def _scope_clause(model: Any, scope_chain: list[tuple[str, str | None]]) -> Any:
    """把 ScopeContext.chain() 转成 SQL 条件（global 的 scope_id 必须为 NULL）。"""
    clauses = []
    for scope_type, scope_id in scope_chain:
        if scope_id is None:
            clauses.append(and_(
                model.scope_type == scope_type,
                model.scope_id.is_(None),
            ))
        else:
            clauses.append(and_(
                model.scope_type == scope_type,
                model.scope_id == scope_id,
            ))
    return or_(*clauses) if clauses else model.scope_type == "global"


class ConflictCandidateFinder:
    """三路候选发现器（只读，不写任何表）。"""

    def __init__(self, db: Session, *, limit: int = 5, retrieval_min_score: float = 0.3) -> None:
        self._db: Session = db
        self._limit: int = max(1, limit)
        self._retrieval_min_score: float = retrieval_min_score

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def discover(
        self,
        proposal: Any,
        *,
        scope_chain: list[tuple[str, str | None]],
        scope_context: Any | None = None,
    ) -> CandidateDiscovery:
        """执行三路发现并合并去重。

        Args:
            proposal: MemoryProposal（读取 canonical_key / scope / content / graph_facts）。
            scope_chain: ScopeContext.chain() 结果，用于图谱与召回的范围约束。
            scope_context: 传给 UnifiedRetriever 的 ScopeContext；为空时退化为
                提案自身 scope 组成的单跳链。
        """
        result = CandidateDiscovery()
        merged: dict[str, ConflictCandidate] = {}
        proposal_hash = proposal.content_hash or proposal.content_hash or proposal.compute_content_hash()

        # 图谱查询的 Scope 集合必须包含提案自身 Scope：召回链只覆盖运行时可授权
        # 范围（thread→global），而"同键/同关系"冲突可能落在提案自己的 project /
        # workspace scope 上。
        graph_chain = self._merge_chain(
            [(proposal.scope_type or "global", proposal.scope_id)], scope_chain,
        )

        self._collect_exact(proposal, merged, result)
        self._collect_retrieval(proposal, scope_chain, scope_context, merged, result)
        self._collect_graph(proposal, graph_chain, merged, result)

        visible = self._visible_ids(list(merged))
        candidates: list[ConflictCandidate] = []
        for memory_id, candidate in merged.items():
            if memory_id not in visible:
                result.notes.append(f"candidate_blocked:{memory_id}")
                continue
            record = self._db.get(MemoryRecord, memory_id)
            if record is None:
                continue
            if (record.content_hash or "") == proposal_hash and proposal_hash:
                # 内容完全相同 → 属于 reinforce，不是冲突对象。
                result.notes.append(f"candidate_identical:{memory_id}")
                continue
            candidates.append(candidate)

        candidates.sort(key=lambda item: (-item.retrieval_score, item.memory_id))
        result.candidates = candidates[: self._limit]
        if len(candidates) > self._limit:
            result.notes.append(f"candidates_truncated:{len(candidates) - self._limit}")
        return result

    @staticmethod
    def _merge_chain(
        first: list[tuple[str, str | None]],
        second: list[tuple[str, str | None]],
    ) -> list[tuple[str, str | None]]:
        """合并 Scope 链并去重（保持先后顺序，确定性）。"""
        merged: list[tuple[str, str | None]] = []
        for item in [*first, *second]:
            if item not in merged:
                merged.append(item)
        return merged

    # ------------------------------------------------------------------
    # 1. 精确键
    # ------------------------------------------------------------------

    def _collect_exact(
        self,
        proposal: Any,
        merged: dict[str, ConflictCandidate],
        result: CandidateDiscovery,
    ) -> None:
        query = self._db.query(MemoryRecord).filter(
            MemoryRecord.canonical_key == proposal.canonical_key,
            MemoryRecord.scope_type == proposal.scope_type,
            MemoryRecord.lifecycle_state.in_(list(_CONFLICT_STATES)),
            MemoryRecord.validity_state == ValidityState.VALID.value,
        )
        if proposal.scope_id is not None:
            query = query.filter(MemoryRecord.scope_id == proposal.scope_id)
        else:
            query = query.filter(MemoryRecord.scope_id.is_(None))
        for record in query.order_by(MemoryRecord.updated_at.desc()).limit(self._limit).all():
            result.exact_conflict = True
            self._merge_candidate(
                merged, record, SOURCE_EXACT, retrieval_score=1.0,
            )

    # ------------------------------------------------------------------
    # 2. 统一检索
    # ------------------------------------------------------------------

    def _collect_retrieval(
        self,
        proposal: Any,
        scope_chain: list[tuple[str, str | None]],
        scope_context: Any | None,
        merged: dict[str, ConflictCandidate],
        result: CandidateDiscovery,
    ) -> None:
        query = (proposal.content or "").strip()
        if not query:
            return
        from aiive.memory.recall_models import ScopeContext
        from aiive.retrieval.retrieval_types import RetrievalMode, RetrievalRequest
        from aiive.retrieval.unified_retriever import UnifiedRetriever

        scope = scope_context
        if scope is None:
            thread_id = next(
                (sid for stype, sid in scope_chain if stype == "thread" and sid), None,
            )
            scope = ScopeContext(thread_id=thread_id)
        request = RetrievalRequest(
            query=query[:400],
            mode=RetrievalMode.AUTO,
            scope_context=scope,
            thread_id=getattr(scope, "thread_id", None),
            source_types=["memory_record"],
            max_results=self._limit * 2,
        )
        try:
            retrieved = UnifiedRetriever(self._db).retrieve(request)
        except Exception:
            # 检索失败只是少一路证据，不能当成"没有冲突"。
            logger.exception("冲突候选：统一检索失败，降级为精确键 + 图谱两路")
            result.notes.append("retrieval_failed")
            return
        if retrieved.degraded:
            result.notes.append("retrieval_degraded")
        for hit in retrieved.hits:
            if hit.source_type != "memory_record":
                continue
            memory_id = hit.memory_record_id or hit.source_id
            if not memory_id:
                continue
            if hit.final_score < self._retrieval_min_score:
                continue
            record = self._db.get(MemoryRecord, memory_id)
            if record is None:
                continue
            self._merge_candidate(
                merged, record, SOURCE_RETRIEVAL, retrieval_score=hit.final_score,
            )

    # ------------------------------------------------------------------
    # 3. 图谱关系
    # ------------------------------------------------------------------

    def _collect_graph(
        self,
        proposal: Any,
        scope_chain: list[tuple[str, str | None]],
        merged: dict[str, ConflictCandidate],
        result: CandidateDiscovery,
    ) -> None:
        facts: list[GraphFact] = list(proposal.graph_facts or [])
        if not facts:
            return
        for fact in facts:
            if not fact.is_complete():
                continue
            entity = self._find_entity(fact.subject, scope_chain)
            if entity is None:
                continue
            family = predicate_family(fact.predicate)
            if not family:
                continue
            relations = (
                self._db.query(GraphRelation)
                .filter(
                    GraphRelation.status == "active",
                    GraphRelation.subject_entity_id == entity.id,
                    GraphRelation.predicate.in_(list(family)),
                    _scope_clause(GraphRelation, scope_chain),
                )
                .order_by(GraphRelation.confidence.desc(), GraphRelation.recorded_at.desc())
                .limit(self._limit * 4)
                .all()
            )
            for relation in relations:
                other_object = self._entity_display(relation.object_entity_id)
                result.graph_evidence.append({
                    "relation_id": relation.id,
                    "source_memory_id": relation.source_memory_id,
                    "subject": entity.display_name,
                    "predicate": relation.predicate,
                    "object": other_object,
                    "new_object": fact.object,
                    "cardinality": cardinality(relation.predicate),
                    "object_differs": normalize_entity_name(other_object)
                    != normalize_entity_name(fact.object),
                    "scope_type": relation.scope_type,
                    "scope_id": relation.scope_id,
                    "confidence": round(float(relation.confidence or 0.0), 4),
                    "valid_from": relation.valid_from.isoformat() if relation.valid_from else None,
                })
                record = self._db.get(MemoryRecord, relation.source_memory_id)
                if record is None:
                    continue
                candidate = self._merge_candidate(
                    merged, record, SOURCE_GRAPH, retrieval_score=0.0,
                )
                candidate.graph_relations.append({
                    "relation_id": relation.id,
                    "subject": entity.display_name,
                    "predicate": relation.predicate,
                    "object": other_object,
                    "cardinality": cardinality(relation.predicate),
                })

    def _find_entity(
        self, name: str, scope_chain: list[tuple[str, str | None]],
    ) -> GraphEntity | None:
        """按规范化名找实体；未命中时做有界别名匹配（与 graph_expansion 同口径）。"""
        normalized = normalize_entity_name(name)
        if not normalized:
            return None
        clause = _scope_clause(GraphEntity, scope_chain)
        exact = (
            self._db.query(GraphEntity)
            .filter(GraphEntity.normalized_name == normalized, clause)
            .order_by(GraphEntity.updated_at.desc())
            .first()
        )
        if exact is not None:
            return exact
        candidates = (
            self._db.query(GraphEntity)
            .filter(clause)
            .limit(_ALIAS_SCAN_LIMIT)
            .all()
        )
        for entity in candidates:
            aliases = entity.aliases or []
            if any(normalize_entity_name(alias) == normalized for alias in aliases):
                return entity
        return None

    def _entity_display(self, entity_id: str) -> str:
        entity = self._db.get(GraphEntity, entity_id)
        return entity.display_name if entity is not None else ""

    # ------------------------------------------------------------------
    # 合并与校验
    # ------------------------------------------------------------------

    def _merge_candidate(
        self,
        merged: dict[str, ConflictCandidate],
        record: MemoryRecord,
        source: str,
        *,
        retrieval_score: float,
    ) -> ConflictCandidate:
        existing = merged.get(record.id)
        if existing is not None:
            if source not in existing.sources:
                existing.sources.append(source)
            existing.retrieval_score = max(existing.retrieval_score, retrieval_score)
            return existing
        candidate = ConflictCandidate(
            memory_id=record.id,
            content=record.content or "",
            canonical_key=record.canonical_key or "",
            memory_type=record.memory_type or "",
            scope_type=record.scope_type or "global",
            scope_id=record.scope_id,
            record_version=int(record.record_version or 1),
            confidence=float(record.confidence or 0.0),
            trust_level=record.trust_level or "",
            sources=[source],
            retrieval_score=retrieval_score,
        )
        merged[record.id] = candidate
        return candidate

    def _visible_ids(self, memory_ids: list[str]) -> set[str]:
        """复用遗忘 Saga 的屏蔽判定；判定失败时 fail-closed（全部不可见）。"""
        if not memory_ids:
            return set()
        try:
            from aiive.forget.visibility_service import ForgetVisibilityService

            return set(ForgetVisibilityService.filter_memory_ids(self._db, memory_ids))
        except Exception:
            logger.exception("冲突候选：遗忘屏蔽判定失败，按 fail-closed 处理")
            return set()
