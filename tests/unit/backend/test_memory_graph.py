"""时序知识图谱：投影、扩展、预算装配、重建与提取侧证据校验。

全部离线：SQLite 测试库 + 纯函数，不访问网络、不需要 API Key。
"""
from __future__ import annotations

from datetime import UTC, datetime

from aiive.db.models import GraphEntity, GraphRelation, MemoryRecord, OutboxJob
from aiive.memory.graph_expansion import GraphExpansionService, render_expansion_block
from aiive.memory.graph_projection import GraphProjectionService
from aiive.memory.graph_rebuild import GraphRebuildService
from aiive.memory.graph_types import GraphConfig, GraphFact
from aiive.memory.memory_extractor import UnifiedMemoryExtractor
from aiive.memory.memory_mutation import MemoryMutationExecutor
from aiive.memory.memory_store import MemoryStore
from aiive.memory.memory_types import EvidenceItem, MemoryProposal, TrustLevel
from aiive.memory.proposal_normalizer import ProposalNormalizer


def _fact(
    subject: str = "用户",
    predicate: str = "works_on",
    object_: str = "AIive",
    confidence: float = 0.9,
    span: str = "我在做 AIive",
    valid_from: datetime | None = None,
) -> GraphFact:
    return GraphFact(
        subject=subject,
        predicate=predicate,
        object=object_,
        confidence=confidence,
        source_span=span,
        valid_from=valid_from,
    )


def _record(
    db,
    *,
    facts: list[GraphFact],
    key: str = "project.aiive.stack",
    scope: tuple[str, str | None] = ("project", "aiive"),
    lifecycle: str = "active",
    validity: str = "valid",
    content: str = "我在做 AIive 项目",
    memory_type: str = "project",
) -> MemoryRecord:
    proposal = MemoryProposal(
        memory_type=memory_type,
        canonical_key=key,
        scope_type=scope[0],
        scope_id=scope[1],
        content=content,
        graph_facts=facts,
        evidence=[EvidenceItem(
            source_type="user_message",
            trust_level=TrustLevel.TRUSTED.value,
            relation="supports",
            content_span=facts[0].source_span if facts else None,
        )],
        confidence=0.9,
    )
    record = MemoryStore(db).create_record(
        proposal, lifecycle_state=lifecycle, validity_state=validity,
    )
    db.flush()
    return record


def _refresh(db, record: MemoryRecord) -> dict:
    result = GraphProjectionService(db).refresh(record.id, record.record_version)
    db.flush()
    return result


def _active_relations(db, memory_id: str) -> list[GraphRelation]:
    return (
        db.query(GraphRelation)
        .filter(
            GraphRelation.source_memory_id == memory_id,
            GraphRelation.status == "active",
        )
        .all()
    )


# ============================================================================
# 写侧投影
# ============================================================================


def test_projection_creates_entities_and_relations(db_session):
    record = _record(db_session, facts=[_fact()])
    result = _refresh(db_session, record)

    assert result["status"] == "projected"
    assert result["created"] == 1
    relations = _active_relations(db_session, record.id)
    assert len(relations) == 1
    relation = relations[0]
    assert relation.predicate == "works_on"
    assert relation.confidence == 0.9
    assert relation.source_span == "我在做 AIive"
    assert relation.scope_type == "project"
    assert relation.scope_id == "aiive"
    # 事实有效时间未知时保持 NULL，不得用写入时间伪装
    assert relation.valid_from is None
    assert relation.recorded_at is not None
    entities = db_session.query(GraphEntity).all()
    assert {e.normalized_name for e in entities} == {"用户", "aiive"}


def test_projection_is_idempotent(db_session):
    record = _record(db_session, facts=[_fact()])
    _refresh(db_session, record)
    first = [r.id for r in _active_relations(db_session, record.id)]

    _refresh(db_session, record)
    second = [r.id for r in _active_relations(db_session, record.id)]

    assert first == second
    assert db_session.query(GraphRelation).count() == 1


def test_candidate_is_not_fact_source(db_session):
    record = _record(db_session, facts=[_fact()], lifecycle="candidate")
    result = _refresh(db_session, record)

    assert result["status"] == "candidate_skipped"
    assert db_session.query(GraphRelation).count() == 0


def test_superseded_source_closes_relations(db_session):
    record = _record(db_session, facts=[_fact()])
    _refresh(db_session, record)

    record.validity_state = "superseded"
    record.record_version += 1
    db_session.flush()
    result = _refresh(db_session, record)

    assert result["status"] == "record_invalid_closed"
    rows = db_session.query(GraphRelation).all()
    assert rows and all(row.status == "closed" for row in rows)
    assert all(row.closed_reason == "record_superseded" for row in rows)


def test_forgotten_purges_relations_and_entities(db_session):
    record = _record(db_session, facts=[_fact()])
    _refresh(db_session, record)
    assert db_session.query(GraphRelation).count() == 1

    record.lifecycle_state = "forgotten"
    record.record_version += 1
    db_session.flush()
    result = _refresh(db_session, record)

    assert result["status"] == "forgotten_purged"
    assert db_session.query(GraphRelation).count() == 0
    assert db_session.query(GraphEntity).count() == 0


def test_version_update_retracts_only_removed_fact(db_session):
    keep = _fact(predicate="works_on", object_="AIive")
    drop = _fact(predicate="prefers", object_="Python")
    record = _record(db_session, facts=[keep, drop])
    _refresh(db_session, record)
    assert len(_active_relations(db_session, record.id)) == 2

    record.graph_facts = [keep.model_dump(mode="json")]
    record.record_version += 1
    db_session.flush()
    _refresh(db_session, record)

    active = _active_relations(db_session, record.id)
    assert len(active) == 1
    assert active[0].predicate == "works_on"
    # 事实被删除（来源不再主张）与事实被取代是两种终态：撤回不写事实结束时间。
    retracted = (
        db_session.query(GraphRelation)
        .filter(
            GraphRelation.source_memory_id == record.id,
            GraphRelation.status == "retracted",
        )
        .all()
    )
    assert [row.predicate for row in retracted] == ["prefers"]
    assert retracted[0].closed_reason == "fact_removed"
    assert retracted[0].valid_to is None


def test_multi_valued_relations_coexist(db_session):
    """多值谓词（如"在做哪些项目"）允许多个客体同时有效，不得互相关闭。"""
    old_record = _record(
        db_session,
        facts=[_fact(predicate="works_on", object_="旧项目")],
        key="project.old.stack",
        content="我在做旧项目",
    )
    _refresh(db_session, old_record)
    old_relation = _active_relations(db_session, old_record.id)[0]

    new_record = _record(
        db_session,
        facts=[_fact(predicate="works_on", object_="AIive")],
        key="project.aiive.stack",
        content="我在做 AIive",
    )
    _refresh(db_session, new_record)

    db_session.refresh(old_relation)
    assert old_relation.status == "active"
    assert old_relation.superseded_by_id is None
    assert len(_active_relations(db_session, old_record.id)) == 1
    assert len(_active_relations(db_session, new_record.id)) == 1
    assert db_session.query(GraphRelation).count() == 2


def test_unknown_predicate_defaults_to_multi_valued(db_session):
    """未声明的谓词按多值处理：即使客体不同也不关闭旧关系。"""
    old_record = _record(
        db_session,
        facts=[_fact(predicate="likes_language", object_="Rust")],
        key="pref.lang.rust", content="我喜欢 Rust",
    )
    _refresh(db_session, old_record)
    old_relation = _active_relations(db_session, old_record.id)[0]

    new_record = _record(
        db_session,
        facts=[_fact(predicate="likes_language", object_="Go")],
        key="pref.lang.go", content="我喜欢 Go",
    )
    _refresh(db_session, new_record)

    db_session.refresh(old_relation)
    assert old_relation.status == "active"
    assert db_session.query(GraphRelation).filter(GraphRelation.status == "active").count() == 2


def test_single_valued_conflict_closes_only_with_authoritative_supersede(db_session):
    """单值谓词 + 记忆层权威取代：结束旧关系有效期并保留历史。"""
    old_record = _record(
        db_session,
        facts=[_fact(predicate="works_at", object_="旧公司")],
        key="identity.old.company", content="我在旧公司工作",
    )
    _refresh(db_session, old_record)
    old_relation = _active_relations(db_session, old_record.id)[0]

    new_record = _record(
        db_session,
        facts=[_fact(predicate="works_at", object_="AIive")],
        key="identity.current.company", content="我现在在 AIive 工作",
    )
    old_record.validity_state = "superseded"
    old_record.superseded_by = new_record.id
    db_session.flush()
    _refresh(db_session, new_record)
    new_relation = _active_relations(db_session, new_record.id)[0]

    db_session.refresh(old_relation)
    assert old_relation.status == "closed"
    assert old_relation.closed_reason == "superseded_by_newer_fact"
    assert old_relation.superseded_by_id == new_relation.id
    assert old_relation.valid_to is not None
    # 历史行保留，可追溯
    assert db_session.query(GraphRelation).count() == 2


def test_single_valued_conflict_without_authority_keeps_both_active(db_session):
    """单值谓词但记忆层没有取代结论：两条都保留，交回记忆层裁决。"""
    old_record = _record(
        db_session,
        facts=[_fact(predicate="works_at", object_="旧公司")],
        key="identity.old.company", content="我在旧公司工作",
    )
    _refresh(db_session, old_record)
    old_relation = _active_relations(db_session, old_record.id)[0]

    new_record = _record(
        db_session,
        facts=[_fact(predicate="works_at", object_="AIive")],
        key="identity.current.company", content="我现在在 AIive 工作",
    )
    _refresh(db_session, new_record)

    db_session.refresh(old_relation)
    assert old_relation.status == "active"
    assert old_relation.superseded_by_id is None
    assert db_session.query(GraphRelation).filter(GraphRelation.status == "active").count() == 2


def test_predicate_alias_family_shares_single_valued_rule(db_session):
    """同族写法（employer / works_at）按同一单值语义比较。"""
    old_record = _record(
        db_session,
        facts=[_fact(predicate="employer", object_="旧公司")],
        key="identity.old.company", content="我在旧公司工作",
    )
    _refresh(db_session, old_record)
    old_relation = _active_relations(db_session, old_record.id)[0]

    new_record = _record(
        db_session,
        facts=[_fact(predicate="works_at", object_="AIive")],
        key="identity.current.company", content="我现在在 AIive 工作",
    )
    old_record.validity_state = "superseded"
    old_record.superseded_by = new_record.id
    db_session.flush()
    _refresh(db_session, new_record)

    db_session.refresh(old_relation)
    assert old_relation.status == "closed"


def test_projection_skips_low_confidence_and_missing_span(db_session):
    weak = _fact(confidence=0.3, object_="弱关系")
    no_span = _fact(object_="无证据", span="")
    record = _record(db_session, facts=[weak, no_span])
    result = _refresh(db_session, record)

    assert result["status"] == "no_facts"
    assert db_session.query(GraphRelation).count() == 0


def test_enqueue_projection_adds_graph_refresh_job(db_session):
    record = _record(db_session, facts=[_fact()])
    MemoryMutationExecutor(db_session).enqueue_projection(record, "memory.created")
    db_session.flush()

    jobs = db_session.query(OutboxJob).filter_by(job_type="memory_graph_refresh").all()
    assert len(jobs) == 1
    assert jobs[0].payload["memory_id"] == record.id
    assert jobs[0].payload["record_version"] == record.record_version


# ============================================================================
# 读侧扩展
# ============================================================================


def test_expansion_returns_seeded_relation(db_session):
    record = _record(db_session, facts=[_fact()])
    _refresh(db_session, record)

    result = GraphExpansionService(db_session).expand(
        seed_memory_ids=[record.id],
        query="AIive 进展",
        scope_chain=[("project", "aiive"), ("global", None)],
    )

    assert len(result.relations) == 1
    view = result.relations[0]
    assert (view.subject, view.predicate, view.object) == ("用户", "works_on", "AIive")
    assert view.source_memory_id == record.id
    assert view.is_current is True
    assert view.source_span == "我在做 AIive"
    assert view.token_cost >= 1


def test_expansion_skips_superseded_source(db_session):
    record = _record(db_session, facts=[_fact()])
    _refresh(db_session, record)
    record.validity_state = "superseded"
    record.record_version += 1
    db_session.flush()

    result = GraphExpansionService(db_session).expand(
        seed_memory_ids=[record.id],
        query="AIive",
        scope_chain=[("project", "aiive"), ("global", None)],
    )
    assert result.relations == []


def test_expansion_respects_scope(db_session):
    record = _record(db_session, facts=[_fact()], scope=("project", "other"))
    _refresh(db_session, record)

    result = GraphExpansionService(db_session).expand(
        seed_memory_ids=[record.id],
        query="AIive",
        scope_chain=[("project", "aiive"), ("global", None)],
    )
    assert result.relations == []


def test_expansion_applies_confidence_floor(db_session):
    record = _record(db_session, facts=[_fact(confidence=0.65)])
    _refresh(db_session, record)

    relaxed = GraphExpansionService(
        db_session, GraphConfig(min_confidence=0.6),
    ).expand(
        seed_memory_ids=[record.id], query="AIive",
        scope_chain=[("project", "aiive"), ("global", None)],
    )
    strict = GraphExpansionService(
        db_session, GraphConfig(min_confidence=0.95),
    ).expand(
        seed_memory_ids=[record.id], query="AIive",
        scope_chain=[("project", "aiive"), ("global", None)],
    )
    assert len(relaxed.relations) == 1
    assert strict.relations == []


def test_expansion_respects_max_hops(db_session):
    first = _record(
        db_session, facts=[_fact(subject="alpha", object_="beta")],
        key="project.chain.a", content="alpha 和 beta 一起工作",
    )
    second = _record(
        db_session, facts=[_fact(subject="beta", object_="gamma")],
        key="project.chain.b", content="beta 和 gamma 一起工作",
    )
    _refresh(db_session, first)
    _refresh(db_session, second)

    one_hop = GraphExpansionService(
        db_session, GraphConfig(max_hops=1),
    ).expand(
        seed_memory_ids=[], query="alpha",
        scope_chain=[("project", "aiive"), ("global", None)],
    )
    two_hops = GraphExpansionService(
        db_session, GraphConfig(max_hops=2),
    ).expand(
        seed_memory_ids=[], query="alpha",
        scope_chain=[("project", "aiive"), ("global", None)],
    )

    assert [(r.subject, r.object) for r in one_hop.relations] == [("alpha", "beta")]
    assert {r.object for r in two_hops.relations} == {"beta", "gamma"}
    assert one_hop.hops_used == 1


def test_expansion_matches_query_entity_without_seed_memory(db_session):
    record = _record(db_session, facts=[_fact(subject="Daniel", object_="AIive")])
    _refresh(db_session, record)

    result = GraphExpansionService(db_session).expand(
        seed_memory_ids=[],
        query="Daniel 最近在做什么",
        scope_chain=[("project", "aiive"), ("global", None)],
    )
    assert result.matched_query_entities == ["Daniel"]
    assert len(result.relations) == 1


def test_expansion_returns_empty_without_relations(db_session):
    result = GraphExpansionService(db_session).expand(
        seed_memory_ids=[], query="无关查询",
        scope_chain=[("global", None)],
    )
    assert result.is_empty()
    assert render_expansion_block(result.relations, 500) == ("", 0)


# ============================================================================
# 预算装配
# ============================================================================


def test_render_block_is_atomic_under_budget(db_session):
    record = _record(db_session, facts=[_fact()])
    _refresh(db_session, record)
    result = GraphExpansionService(db_session).expand(
        seed_memory_ids=[record.id], query="AIive",
        scope_chain=[("project", "aiive"), ("global", None)],
    )

    # 预算极小：宁可什么都不注入，也不截断关系定义
    text, used = render_expansion_block(result.relations, 5)
    assert text == "" and used == 0

    text, used = render_expansion_block(result.relations, 200)
    assert text.startswith("## 时序关系")
    assert "—[works_on]→" in text
    assert "来源 memory:" in text
    assert "证据：" in text
    assert used <= 200


# ============================================================================
# 重建
# ============================================================================


def test_rebuild_restores_deleted_relations(db_session):
    record = _record(db_session, facts=[_fact()])
    _refresh(db_session, record)
    db_session.query(GraphRelation).delete()
    db_session.flush()
    assert db_session.query(GraphRelation).count() == 0

    report = GraphRebuildService(db_session).rebuild()
    db_session.flush()

    assert report["orphan_relations_removed"] == 0
    assert db_session.query(GraphRelation).count() == 1
    assert report["verify"]["active_from_invalid_source"] == 0


def test_verify_flags_active_relation_from_forgotten_source(db_session):
    record = _record(db_session, facts=[_fact()])
    _refresh(db_session, record)
    # 模拟投影滞后：记录已被遗忘，但关系仍是 active
    record.lifecycle_state = "forgotten"
    db_session.flush()

    report = GraphRebuildService(db_session).verify()
    assert report["active_from_invalid_source"] == 1


# ============================================================================
# 提取侧证据校验
# ============================================================================


def test_verified_graph_facts_requires_verbatim_span():
    from aiive.memory.graph_types import GraphFactPayload

    payloads = [
        GraphFactPayload(
            subject="用户", predicate="works_on", object="AIive",
            confidence=0.9, source_span="我在做 AIive",
        ),
        GraphFactPayload(
            subject="用户", predicate="owns", object="公司",
            confidence=0.9, source_span="这句话原文里没有",
        ),
    ]
    facts = UnifiedMemoryExtractor._verified_graph_facts(
        payloads, user_message="我在做 AIive 项目", content="我在做 AIive 项目",
    )
    assert len(facts) == 1
    assert facts[0].predicate == "works_on"


def test_verified_graph_facts_parses_dates_and_keeps_unknown_empty():
    from aiive.memory.graph_types import GraphFactPayload

    payloads = [
        GraphFactPayload(
            subject="用户", predicate="joined", object="AIive",
            confidence=0.9, source_span="我 2026-01-02 加入 AIive",
            valid_from="2026-01-02",
        ),
        GraphFactPayload(
            subject="用户", predicate="likes", object="茶",
            confidence=0.9, source_span="我喜欢喝茶",
            valid_from="说不清",
        ),
    ]
    facts = UnifiedMemoryExtractor._verified_graph_facts(
        payloads, user_message="我 2026-01-02 加入 AIive，我喜欢喝茶", content="",
    )
    assert len(facts) == 2
    joined = next(f for f in facts if f.predicate == "joined")
    likes = next(f for f in facts if f.predicate == "likes")
    assert joined.valid_from is not None and joined.valid_from.year == 2026
    assert likes.valid_from is None


def test_normalizer_carries_graph_facts():
    fact = _fact()
    result = ProposalNormalizer().normalize(
        content="我在做 AIive",
        memory_type_hint="project",
        memory_key_hint="project.aiive.stack",
        confidence=0.9,
        evidence=[{"source_type": "user_message", "trust_level": "trusted"}],
        graph_facts=[fact],
    )
    assert result.proposal is not None
    assert len(result.proposal.graph_facts) == 1
    assert result.proposal.graph_facts[0].object == "AIive"


def test_store_persists_graph_facts_as_canonical_data(db_session):
    record = _record(db_session, facts=[_fact()])
    stored = db_session.get(MemoryRecord, record.id)
    assert stored is not None
    assert stored.graph_facts and stored.graph_facts[0]["predicate"] == "works_on"
    # 事实时间未知时保持 None（不落写入时刻）
    assert stored.graph_facts[0]["valid_from"] is None


def test_history_is_attached_for_superseded_relation(db_session):
    old_record = _record(
        db_session, facts=[_fact(predicate="works_at", object_="旧公司")],
        key="identity.old.company", content="我在旧公司工作",
    )
    _refresh(db_session, old_record)
    new_record = _record(
        db_session, facts=[_fact(predicate="works_at", object_="AIive")],
        key="identity.current.company", content="我现在在 AIive 工作",
    )
    old_record.validity_state = "superseded"
    old_record.superseded_by = new_record.id
    db_session.flush()
    _refresh(db_session, new_record)

    result = GraphExpansionService(db_session).expand(
        seed_memory_ids=[new_record.id], query="AIive",
        scope_chain=[("project", "aiive"), ("global", None)],
    )
    current = [r for r in result.relations if r.is_current]
    history = [r for r in result.relations if not r.is_current]
    assert len(current) == 1 and current[0].object == "AIive"
    assert len(history) == 1 and history[0].object == "旧公司"
    assert history[0].valid_to is not None


def test_scope_chain_global_relation_visible(db_session):
    record = _record(
        db_session, facts=[_fact()],
        scope=("global", None), key="knowledge.aiive",
        content="AIive 是一个项目", memory_type="knowledge",
    )
    _refresh(db_session, record)

    result = GraphExpansionService(db_session).expand(
        seed_memory_ids=[record.id], query="AIive",
        scope_chain=[("project", "aiive"), ("global", None)],
    )
    assert len(result.relations) == 1
    assert result.relations[0].scope_type == "global"


def test_recorded_at_differs_from_fact_time(db_session):
    moment = datetime(2025, 5, 1, tzinfo=UTC)
    record = _record(db_session, facts=[_fact(valid_from=moment)])
    _refresh(db_session, record)

    relation = _active_relations(db_session, record.id)[0]
    assert relation.valid_from is not None
    assert relation.recorded_at > relation.valid_from


# ============================================================================
# Outbox 消费与遗忘清理
# ============================================================================


def _claimed(payload: dict) -> object:
    from aiive.worker.outbox_dto import ClaimedJob

    return ClaimedJob(
        id="job-1",
        job_type="memory_graph_refresh",
        payload=payload,
        trace_id="trace-1",
        retry_count=0,
        max_retries=3,
        claim_token="token",
        schema_version=1,
        worker_id="worker-1",
    )


def test_outbox_handler_projects_graph(db_session, monkeypatch):
    """Handler 必须把 payload 的 (memory_id, version) 交给投影服务。

    真实投影已在其它用例覆盖；这里只验证 Outbox 接线参数不丢、不猜。
    """
    from aiive.memory.graph_projection import GraphProjectionService
    from aiive.worker.outbox_dto import HandlerOutcome
    from aiive.worker.outbox_handlers import handle_memory_graph_refresh

    calls: list[tuple[str, int]] = []

    def _refresh(self, memory_id, record_version):
        calls.append((memory_id, record_version))
        return {"status": "projected"}

    monkeypatch.setattr(GraphProjectionService, "refresh", _refresh)
    result = handle_memory_graph_refresh(
        _claimed({"memory_id": "mem-1", "record_version": 3}),
    )
    assert result.outcome == HandlerOutcome.COMPLETED
    assert calls == [("mem-1", 3)]


def test_outbox_handler_rejects_invalid_payload(db_session):
    from aiive.worker.outbox_handlers import handle_memory_graph_refresh
    from aiive.worker.outbox_dto import HandlerOutcome

    result = handle_memory_graph_refresh(_claimed({"memory_id": "", "record_version": 0}))
    assert result.outcome == HandlerOutcome.NON_RETRYABLE


def test_forget_scrub_removes_graph_rows_and_facts(db_session):
    from aiive.worker.handlers_forget import _scrub_batch

    record = _record(db_session, facts=[_fact()])
    _refresh(db_session, record)
    assert db_session.query(GraphRelation).count() == 1

    _scrub_batch(db_session, [record.id])
    db_session.flush()

    assert db_session.query(GraphRelation).count() == 0
    assert db_session.query(GraphEntity).count() == 0
    db_session.refresh(record)
    assert record.graph_facts is None
    assert record.content == "[forgotten]"


def test_write_service_persists_facts_and_enqueues_graph_job(db_session):
    """完整写入链：Gate 通过 → canonical 落库 → 图谱投影 job 入队。"""
    from aiive.context.run_context import RunContext
    from aiive.memory.memory_write_service import MemoryWriteService

    proposal = MemoryProposal(
        memory_type="project",
        canonical_key="project.aiive.stack",
        scope_type="project",
        scope_id="aiive",
        content="我在做 AIive 项目",
        graph_facts=[_fact()],
        evidence=[EvidenceItem(
            source_type="user_message",
            trust_level=TrustLevel.TRUSTED.value,
            relation="supports",
            content_span="我在做 AIive",
        )],
        confidence=0.95,
    )
    proposal.compute_request_idempotency()

    result = MemoryWriteService(db_session).write(
        proposal,
        RunContext(thread_id="thread-1", trace_id="trace-1", source="test"),
    )
    db_session.flush()

    assert result.written is True
    record = db_session.get(MemoryRecord, result.memory_id)
    assert record is not None and record.graph_facts
    jobs = db_session.query(OutboxJob).filter_by(job_type="memory_graph_refresh").all()
    assert any(job.payload["memory_id"] == result.memory_id for job in jobs)

    # 投影消费后关系可用
    GraphProjectionService(db_session).refresh(record.id, record.record_version)
    db_session.flush()
    assert len(_active_relations(db_session, record.id)) == 1


# ============================================================================
# 关系基数：单值 / 多值 / 未知谓词
# ============================================================================


def test_predicate_cardinality_registry():
    """只有明确声明的谓词是单值；其余（含未知）一律多值。"""
    from aiive.memory.graph_predicates import (
        canonical_predicate,
        cardinality,
        is_single_valued,
        predicate_family,
    )

    assert cardinality("works_at") == "single"
    assert cardinality("employer") == "single"
    assert canonical_predicate("employer") == "works_at"
    assert is_single_valued("WORKS_AT") is True
    assert cardinality("works_on") == "multi"
    assert cardinality("likes_language") == "multi"
    assert canonical_predicate("likes_language") == "likes_language"
    assert set(predicate_family("works_at")) >= {"works_at", "employer", "employed_by"}


def test_single_valued_predicate_extensible_by_settings(monkeypatch):
    """业务谓词可通过配置补充为单值，无需改代码。"""
    from aiive.config import settings
    from aiive.memory.graph_predicates import cardinality

    assert cardinality("reports_to") == "multi"
    monkeypatch.setattr(settings, "aiive_memory_graph_single_valued_predicates", "reports_to")
    assert cardinality("reports_to") == "single"


def test_multi_valued_objects_are_not_flagged_conflicted(db_session):
    """多值谓词的多个客体是正常并存，不是冲突。"""
    first = _record(
        db_session, facts=[_fact(predicate="likes_language", object_="Rust")],
        key="pref.lang.rust", content="我喜欢 Rust",
    )
    _refresh(db_session, first)
    second = _record(
        db_session, facts=[_fact(predicate="likes_language", object_="Go")],
        key="pref.lang.go", content="我喜欢 Go",
    )
    _refresh(db_session, second)

    result = GraphExpansionService(db_session).expand(
        seed_memory_ids=[first.id, second.id], query="喜欢的语言",
        scope_chain=[("project", "aiive"), ("global", None)],
    )
    current = [item for item in result.relations if item.is_current]
    assert len(current) == 2
    assert all(item.cardinality == "multi" for item in current)
    assert all(item.is_conflicted is False for item in current)
    assert result.conflicted == 0
    text, _ = render_expansion_block(result.relations, 4000)
    assert "冲突关系" not in text


def test_single_valued_multi_object_is_flagged_conflicted(db_session):
    """单值谓词出现两个有效客体：都保留，但标为冲突、不进"当前有效"。"""
    old_record = _record(
        db_session, facts=[_fact(predicate="works_at", object_="旧公司")],
        key="identity.old.company", content="我在旧公司工作",
    )
    _refresh(db_session, old_record)
    new_record = _record(
        db_session, facts=[_fact(predicate="works_at", object_="AIive")],
        key="identity.current.company", content="我现在在 AIive 工作",
    )
    _refresh(db_session, new_record)

    result = GraphExpansionService(db_session).expand(
        seed_memory_ids=[old_record.id, new_record.id], query="公司",
        scope_chain=[("project", "aiive"), ("global", None)],
    )
    current = [item for item in result.relations if item.is_current]
    assert len(current) == 2
    assert all(item.cardinality == "single" for item in current)
    assert all(item.is_conflicted is True for item in current)
    assert result.conflicted == 2

    text, _ = render_expansion_block(result.relations, 4000)
    assert "冲突关系（同一单值关系存在多个有效客体" in text
    # 冲突关系不得混进"当前有效关系"分组
    head = text.split("冲突关系")[0]
    assert "—[works_at]→" not in head


# ============================================================================
# 旧规则误关关系的修复
# ============================================================================


def _legacy_close(db, old_relation, new_relation) -> None:
    """模拟旧规则：仅凭新出现不同客体就关闭更早的关系。"""
    old_relation.status = "closed"
    old_relation.closed_reason = "superseded_by_newer_fact"
    old_relation.superseded_by_id = new_relation.id
    old_relation.valid_to = datetime.now(UTC)
    db.flush()


def test_repair_reopens_multi_valued_wrongly_closed(db_session):
    old_record = _record(
        db_session, facts=[_fact(predicate="works_on", object_="旧项目")],
        key="project.old.stack", content="我在做旧项目",
    )
    _refresh(db_session, old_record)
    old_relation = _active_relations(db_session, old_record.id)[0]
    new_record = _record(
        db_session, facts=[_fact(predicate="works_on", object_="AIive")],
        key="project.aiive.stack", content="我在做 AIive",
    )
    _refresh(db_session, new_record)
    new_relation = _active_relations(db_session, new_record.id)[0]
    _legacy_close(db_session, old_relation, new_relation)

    service = GraphRebuildService(db_session)
    dry = service.repair_overclosed(dry_run=True)
    assert dry["dry_run"] is True and dry["reopened"] == 1
    db_session.refresh(old_relation)
    assert old_relation.status == "closed"  # dry run 不写库

    report = service.repair_overclosed()
    assert report["reopened_multi_valued"] == 1
    db_session.refresh(old_relation)
    assert old_relation.status == "active"
    assert old_relation.closed_reason is None
    assert old_relation.superseded_by_id is None
    assert old_relation.valid_to is None

    # 幂等：再跑一次没有可修复项
    again = service.repair_overclosed()
    assert again["scanned"] == 0 and again["reopened"] == 0


def test_repair_reopens_single_valued_when_source_still_valid(db_session):
    """单值谓词但来源记忆仍有效：恢复为并存 + 冲突，而不是保持误关。"""
    old_record = _record(
        db_session, facts=[_fact(predicate="works_at", object_="旧公司")],
        key="identity.old.company", content="我在旧公司工作",
    )
    _refresh(db_session, old_record)
    old_relation = _active_relations(db_session, old_record.id)[0]
    new_record = _record(
        db_session, facts=[_fact(predicate="works_at", object_="AIive")],
        key="identity.current.company", content="我现在在 AIive 工作",
    )
    _refresh(db_session, new_record)
    new_relation = _active_relations(db_session, new_record.id)[0]
    _legacy_close(db_session, old_relation, new_relation)

    report = GraphRebuildService(db_session).repair_overclosed()
    assert report["reopened_single_valued"] == 1
    db_session.refresh(old_relation)
    assert old_relation.status == "active"

    result = GraphExpansionService(db_session).expand(
        seed_memory_ids=[old_record.id, new_record.id], query="公司",
        scope_chain=[("project", "aiive"), ("global", None)],
    )
    assert result.conflicted == 2


def test_repair_keeps_authoritative_supersede(db_session):
    """来源记忆确实被取代：保留关闭，不猜测性恢复。"""
    old_record = _record(
        db_session, facts=[_fact(predicate="works_at", object_="旧公司")],
        key="identity.old.company", content="我在旧公司工作",
    )
    _refresh(db_session, old_record)
    old_relation = _active_relations(db_session, old_record.id)[0]
    new_record = _record(
        db_session, facts=[_fact(predicate="works_at", object_="AIive")],
        key="identity.current.company", content="我现在在 AIive 工作",
    )
    old_record.validity_state = "superseded"
    old_record.superseded_by = new_record.id
    db_session.flush()
    _refresh(db_session, new_record)

    report = GraphRebuildService(db_session).repair_overclosed()
    assert report["kept_authoritative"] == 1
    assert report["reopened"] == 0
    db_session.refresh(old_relation)
    assert old_relation.status == "closed"


def test_repair_skips_relation_whose_fact_was_removed(db_session):
    """来源记忆仍有效但已不再主张该事实：属于正常撤回，不恢复。"""
    old_record = _record(
        db_session, facts=[_fact(predicate="works_on", object_="旧项目")],
        key="project.old.stack", content="我在做旧项目",
    )
    _refresh(db_session, old_record)
    old_relation = _active_relations(db_session, old_record.id)[0]
    new_record = _record(
        db_session, facts=[_fact(predicate="works_on", object_="AIive")],
        key="project.aiive.stack", content="我在做 AIive",
    )
    _refresh(db_session, new_record)
    new_relation = _active_relations(db_session, new_record.id)[0]
    _legacy_close(db_session, old_relation, new_relation)

    old_record.graph_facts = []
    old_record.record_version += 1
    db_session.flush()

    report = GraphRebuildService(db_session).repair_overclosed()
    assert report["skipped_fact_removed"] == 1 and report["reopened"] == 0
    db_session.refresh(old_relation)
    assert old_relation.status == "closed"


def test_rebuild_repairs_legacy_closure_and_verify_is_clean(db_session):
    old_record = _record(
        db_session, facts=[_fact(predicate="works_on", object_="旧项目")],
        key="project.old.stack", content="我在做旧项目",
    )
    _refresh(db_session, old_record)
    old_relation = _active_relations(db_session, old_record.id)[0]
    new_record = _record(
        db_session, facts=[_fact(predicate="works_on", object_="AIive")],
        key="project.aiive.stack", content="我在做 AIive",
    )
    _refresh(db_session, new_record)
    new_relation = _active_relations(db_session, new_record.id)[0]
    _legacy_close(db_session, old_relation, new_relation)

    report = GraphRebuildService(db_session).rebuild()
    assert report["repair"]["reopened_multi_valued"] == 1
    assert report["verify"]["suspect_closed_relations"] == 0
    db_session.refresh(old_relation)
    assert old_relation.status == "active"
    # 重建可重复：再跑一次不产生重复行
    before = db_session.query(GraphRelation).count()
    GraphRebuildService(db_session).rebuild()
    assert db_session.query(GraphRelation).count() == before


def test_repeat_projection_does_not_close_multi_valued_relations(db_session):
    """重复投影 / 重试不会把并存的多个客体关掉（幂等）。"""
    first = _record(
        db_session, facts=[_fact(predicate="likes_language", object_="Rust")],
        key="pref.lang.rust", content="我喜欢 Rust",
    )
    _refresh(db_session, first)
    second = _record(
        db_session, facts=[_fact(predicate="likes_language", object_="Go")],
        key="pref.lang.go", content="我喜欢 Go",
    )
    _refresh(db_session, second)

    for _ in range(3):
        _refresh(db_session, first)
        _refresh(db_session, second)

    rows = db_session.query(GraphRelation).filter(GraphRelation.status == "active").all()
    assert len(rows) == 2
    assert db_session.query(GraphRelation).count() == 2


def test_conflict_detected_when_peer_outside_expansion_window(db_session):
    """扩展窗口只取到一侧时，仍要发现另一侧的单值冲突，不能当成确定事实。"""
    first = _record(
        db_session, facts=[_fact(predicate="works_at", object_="旧公司")],
        key="identity.old.company", content="我在旧公司工作",
    )
    _refresh(db_session, first)
    second = _record(
        db_session, facts=[_fact(predicate="works_at", object_="AIive")],
        key="identity.current.company", content="我现在在 AIive 工作",
    )
    _refresh(db_session, second)

    service = GraphExpansionService(db_session, GraphConfig(batch_size=1))
    result = service.expand(
        seed_memory_ids=[first.id, second.id], query="公司",
        scope_chain=[("project", "aiive"), ("global", None)],
    )
    current = [item for item in result.relations if item.is_current]
    assert len(current) == 1
    assert current[0].is_conflicted is True


def test_relations_endpoint_reports_cardinality_and_conflict(db_session):
    """关系查询接口直接给出基数与冲突标记（跨记忆判定）。"""
    from aiive.api.routes_memories import get_memory_relations

    old_record = _record(
        db_session, facts=[_fact(predicate="works_at", object_="旧公司")],
        key="identity.old.company", content="我在旧公司工作",
    )
    _refresh(db_session, old_record)
    new_record = _record(
        db_session, facts=[_fact(predicate="works_at", object_="AIive")],
        key="identity.current.company", content="我现在在 AIive 工作",
    )
    _refresh(db_session, new_record)
    multi = _record(
        db_session, facts=[_fact(predicate="likes_language", object_="Rust")],
        key="pref.lang.rust", content="我喜欢 Rust",
    )
    _refresh(db_session, multi)

    payload = get_memory_relations(old_record.id, db_session)
    assert payload["memory_id"] == old_record.id
    relation = payload["relations"][0]
    assert relation["cardinality"] == "single"
    assert relation["conflicted"] is True

    multi_payload = get_memory_relations(multi.id, db_session)
    multi_relation = multi_payload["relations"][0]
    assert multi_relation["cardinality"] == "multi"
    assert multi_relation["conflicted"] is False
