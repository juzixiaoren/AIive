"""实体绑定端到端：候选 → 同一次裁决 → 服务端校验 → 正式写入 → 图谱投影。

全部离线：SQLite 共享库 + 脚本化伪 LLM，不访问网络、不需要 API Key。
"""
from __future__ import annotations

import json
import types
import uuid

from aiive.db.models import (
    GraphEntity,
    GraphRelation,
    MemoryConflictCase,
    MemoryIngestionRun,
    MemoryRecord,
    Thread,
    TurnRecord,
)
from aiive.context.run_context import RunContext
from aiive.memory.conflict_adjudication import (
    ACTION_DEFER,
    ACTION_PROCEED,
    BIND_CREATE_NEW,
    BIND_REUSE,
    BIND_UNRESOLVED,
    DECISION_ACCEPT_NEW,
    VERDICT_COMPATIBLE,
    VERDICT_UNCERTAIN,
    ConflictAdjudication,
    ReplyDecision,
)
from aiive.memory.conflict_confirmation import (
    MemoryConflictService,
    ProposalConflictPlan,
)
from aiive.memory.graph_projection import GraphProjectionService
from aiive.memory.graph_types import GraphFact
from aiive.memory.memory_store import MemoryStore
from aiive.memory.memory_types import (
    EvidenceItem,
    LifecycleState,
    MemoryProposal,
    TrustLevel,
    ValidityState,
    WriteOutcome,
)
from aiive.memory.memory_write_service import MemoryWriteService
from aiive.memory.recall_models import ScopeContext


class ScriptedLLM:
    """按调用顺序返回预设内容的伪 LLM（签名兼容 LLMClient.chat）。"""

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses) or ['{"verdict": "compatible"}']
        self.calls = 0
        self.prompts: list[str] = []

    def chat(self, messages, model=None, temperature=None, timeout=None,
             trace_id=None, json_mode=False):
        self.calls += 1
        self.prompts.append(str(messages[-1].get("content", "")))
        index = min(self.calls - 1, len(self._responses) - 1)
        return types.SimpleNamespace(
            content=self._responses[index], finish_reason="stop",
            model="scripted", usage={}, latency_ms=1.0, trace_id=trace_id,
        )


def _new_thread(db, title: str = "实体绑定线程") -> str:
    thread_id = str(uuid.uuid4())
    db.add(Thread(id=thread_id, title=title))
    db.flush()
    return thread_id


def _ctx(thread_id: str) -> RunContext:
    return RunContext(
        thread_id=thread_id, trace_id="trace-binding", source="test",
        execution_mode="user_required",
    )


def _fact(subject: str, predicate: str, object_: str, span: str) -> GraphFact:
    return GraphFact(
        subject=subject, predicate=predicate, object=object_,
        confidence=0.9, source_span=span,
    )


def _proposal(
    key: str,
    content: str,
    *,
    facts: list[GraphFact] | None = None,
    scope: tuple[str, str | None] = ("global", None),
) -> MemoryProposal:
    proposal = MemoryProposal(
        memory_type="user_profile",
        canonical_key=key,
        scope_type=scope[0],
        scope_id=scope[1],
        content=content,
        graph_facts=list(facts or []),
        evidence=[EvidenceItem(
            source_type="user_message",
            trust_level=TrustLevel.TRUSTED.value,
            relation="supports",
            content_span=content,
        )],
        confidence=0.9,
        trust_level=TrustLevel.TRUSTED.value,
    )
    proposal.compute_content_hash()
    proposal.compute_request_idempotency()
    return proposal


def _seed_record(db, proposal: MemoryProposal) -> MemoryRecord:
    record = MemoryStore(db).create_record(
        proposal, lifecycle_state=LifecycleState.ACTIVE.value,
        validity_state=ValidityState.VALID.value,
    )
    db.flush()
    GraphProjectionService(db).refresh(record.id, record.record_version)
    db.flush()
    return record


def _seed_aiive(db) -> tuple[str, MemoryRecord, GraphEntity]:
    """既有图谱里有实体 AIive（由一条 active 记忆投影而来）。"""
    thread_id = _new_thread(db)
    record = _seed_record(db, _proposal(
        "user.employer", "用户在 AIive 工作",
        facts=[_fact("用户", "works_at", "AIive", "用户在 AIive 工作")],
    ))
    entity = (
        db.query(GraphEntity)
        .filter(GraphEntity.normalized_name == "aiive")
        .one()
    )
    return thread_id, record, entity


def _discover(db, proposal: MemoryProposal, thread_id: str):
    return MemoryConflictService.collect_candidates(
        db, [proposal], scope_context=ScopeContext(thread_id=thread_id),
    )[proposal.proposal_id]


def _binding_response(name: str, decision: str, entity_id: str = "", verdict: str = "compatible") -> str:
    return json.dumps({
        "verdict": verdict,
        "reason": "测试",
        "question": "",
        "confidence": 0.8,
        "entity_bindings": [{
            "name": name,
            "decision": decision,
            "entity_id": entity_id,
            "confidence": 0.9,
            "reason": "测试",
        }],
    }, ensure_ascii=False)


def _write_and_project(db, proposal: MemoryProposal, thread_id: str) -> MemoryRecord:
    result = MemoryWriteService(db).write(proposal, _ctx(thread_id))
    db.commit()
    assert result.written is True, result
    record = db.get(MemoryRecord, result.memory_id)
    GraphProjectionService(db).refresh(record.id, record.record_version)
    db.commit()
    return record


def _relation_for(db, memory_id: str) -> GraphRelation:
    return (
        db.query(GraphRelation)
        .filter(GraphRelation.source_memory_id == memory_id)
        .one()
    )


# ---------------------------------------------------------------------------
# 1. 正确绑定
# ---------------------------------------------------------------------------

class TestReuseBinding:
    def test_reuse_binding_keeps_single_entity(self, db):
        thread_id, _old, entity = _seed_aiive(db)
        new = _proposal(
            "project.tech_stack", "AIive 项目的技术栈是 Python",
            facts=[_fact("AIive 项目", "uses_language", "Python", "AIive 项目的技术栈是 Python")],
        )

        discovery = _discover(db, new, thread_id)
        # 两个名称都还没有对应实体；只有 "AIive 项目" 有可复用候选。
        assert discovery.pending_entity_names == ["AIive 项目", "Python"]
        assert [item.entity_id for item in discovery.entity_candidates] == [entity.id]

        fake = ScriptedLLM(_binding_response("AIive 项目", BIND_REUSE, entity.id))
        plans = MemoryConflictService.decide(
            [new], {new.proposal_id: discovery}, llm_client=fake,
        )

        assert fake.calls == 1
        assert plans[new.proposal_id].entity_bindings[0]["accepted"] is True
        assert new.graph_facts[0].subject_entity_id == entity.id

        record = _write_and_project(db, new, thread_id)
        relation = _relation_for(db, record.id)
        assert relation.subject_entity_id == entity.id
        # 没有为 "AIive 项目" 另建节点
        assert (
            db.query(GraphEntity)
            .filter(GraphEntity.normalized_name == "aiive 项目")
            .count()
            == 0
        )
        # 新写法被记成别名，后续同义名称可以直接复用
        refreshed = db.get(GraphEntity, entity.id)
        assert "AIive 项目" in (refreshed.aliases or [])

    def test_alias_is_reused_without_another_llm_call(self, db):
        thread_id, _old, entity = _seed_aiive(db)
        first = _proposal(
            "project.tech_stack", "AIive 项目的技术栈是 Python",
            facts=[_fact("AIive 项目", "uses_language", "Python", "AIive 项目的技术栈是 Python")],
        )
        discovery = _discover(db, first, thread_id)
        MemoryConflictService.decide(
            [first], {first.proposal_id: discovery},
            llm_client=ScriptedLLM(_binding_response("AIive 项目", BIND_REUSE, entity.id)),
        )
        _write_and_project(db, first, thread_id)

        second = _proposal(
            "project.repo", "AIive 项目使用 monorepo",
            facts=[_fact("AIive 项目", "uses_layout", "monorepo", "AIive 项目使用 monorepo")],
        )
        discovery = _discover(db, second, thread_id)

        # 已确认的别名不再需要模型判断；未出现过的 "monorepo" 仍需按名称新建。
        assert discovery.pending_entity_names == ["monorepo"]
        assert discovery.resolved_entity_bindings["AIive 项目"] == entity.id

        plans = MemoryConflictService.decide(
            [second], {second.proposal_id: discovery},
            llm_client=ScriptedLLM('{"verdict": "compatible"}'),
        )

        assert second.graph_facts[0].subject_entity_id == entity.id
        assert plans[second.proposal_id].entity_bindings[0]["reason"] == "alias_match"


# ---------------------------------------------------------------------------
# 2/3. 不误合并 / 歧义
# ---------------------------------------------------------------------------

class TestNoForcedMerge:
    def test_string_containment_alone_creates_new_entity(self, db):
        thread_id, _old, entity = _seed_aiive(db)
        new = _proposal(
            "project.tech_stack", "另一个叫 AIive 项目的产品用 Rust",
            facts=[_fact("AIive 项目", "uses_language", "Rust", "另一个叫 AIive 项目的产品用 Rust")],
        )
        discovery = _discover(db, new, thread_id)
        assert [item.entity_id for item in discovery.entity_candidates] == [entity.id]

        MemoryConflictService.decide(
            [new], {new.proposal_id: discovery},
            llm_client=ScriptedLLM(_binding_response("AIive 项目", BIND_CREATE_NEW)),
        )

        assert new.graph_facts[0].subject_entity_id is None
        record = _write_and_project(db, new, thread_id)
        relation = _relation_for(db, record.id)
        assert relation.subject_entity_id != entity.id
        assert (
            db.query(GraphEntity)
            .filter(GraphEntity.normalized_name == "aiive 项目")
            .count()
            == 1
        )

    def test_ambiguous_candidates_stay_unresolved(self, db):
        thread_id, _old, entity = _seed_aiive(db)
        # 第二个候选实体：同 Scope 下的 "AIive 项目组"
        extra = _seed_record(db, _proposal(
            "team.name", "AIive 项目组有五个人",
            facts=[_fact("AIive 项目组", "has_size", "5", "AIive 项目组有五个人")],
        ))
        team = (
            db.query(GraphEntity)
            .filter(GraphEntity.normalized_name == "aiive 项目组")
            .one()
        )
        assert team.id != entity.id

        new = _proposal(
            "project.tech_stack", "AIive 项目使用 Python",
            facts=[_fact("AIive 项目", "uses_language", "Python", "AIive 项目使用 Python")],
        )
        discovery = _discover(db, new, thread_id)
        candidate_ids = {item.entity_id for item in discovery.entity_candidates}
        assert {entity.id, team.id} <= candidate_ids

        MemoryConflictService.decide(
            [new], {new.proposal_id: discovery},
            llm_client=ScriptedLLM(_binding_response("AIive 项目", BIND_UNRESOLVED)),
        )

        assert new.graph_facts[0].subject_entity_id is None
        record = _write_and_project(db, new, thread_id)
        relation = _relation_for(db, record.id)
        assert relation.subject_entity_id not in (entity.id, team.id)
        assert extra.id  # 既有记忆保持不变


# ---------------------------------------------------------------------------
# 4. 安全校验
# ---------------------------------------------------------------------------

class TestBindingValidation:
    def test_invented_entity_id_is_rejected(self, db):
        thread_id, _old, entity = _seed_aiive(db)
        new = _proposal(
            "project.tech_stack", "AIive 项目使用 Python",
            facts=[_fact("AIive 项目", "uses_language", "Python", "AIive 项目使用 Python")],
        )
        discovery = _discover(db, new, thread_id)

        plans = MemoryConflictService.decide(
            [new], {new.proposal_id: discovery},
            llm_client=ScriptedLLM(
                _binding_response("AIive 项目", BIND_REUSE, str(uuid.uuid4()))
            ),
        )

        binding = plans[new.proposal_id].entity_bindings[0]
        assert binding["accepted"] is False
        assert binding["decision"] == BIND_UNRESOLVED
        assert binding["rejected_reason"] == "entity_not_in_candidates"
        assert new.graph_facts[0].subject_entity_id is None

    def test_cross_scope_entity_is_not_even_a_candidate(self, db):
        thread_id, _old, _entity = _seed_aiive(db)
        # 另一个 Scope（project:p1）里的同名实体
        other = GraphEntity(
            normalized_name="aiive", display_name="AIive",
            entity_type="project", scope_type="project", scope_id="p1",
        )
        db.add(other)
        db.flush()

        new = _proposal(
            "project.tech_stack", "AIive 项目使用 Python",
            facts=[_fact("AIive 项目", "uses_language", "Python", "AIive 项目使用 Python")],
        )
        discovery = _discover(db, new, thread_id)

        assert other.id not in {item.entity_id for item in discovery.entity_candidates}
        plans = MemoryConflictService.decide(
            [new], {new.proposal_id: discovery},
            llm_client=ScriptedLLM(
                _binding_response("AIive 项目", BIND_REUSE, other.id)
            ),
        )
        assert plans[new.proposal_id].entity_bindings[0]["accepted"] is False

    def test_deleted_entity_falls_back_to_name_resolution(self, db):
        thread_id, _old, entity = _seed_aiive(db)
        new = _proposal(
            "project.tech_stack", "AIive 项目使用 Python",
            facts=[_fact("AIive 项目", "uses_language", "Python", "AIive 项目使用 Python")],
        )
        discovery = _discover(db, new, thread_id)
        MemoryConflictService.decide(
            [new], {new.proposal_id: discovery},
            llm_client=ScriptedLLM(_binding_response("AIive 项目", BIND_REUSE, entity.id)),
        )
        assert new.graph_facts[0].subject_entity_id == entity.id

        # 实体在投影前被删除（例如遗忘级联）：绑定失效，退回按名称解析
        db.query(GraphRelation).filter(
            (GraphRelation.subject_entity_id == entity.id)
            | (GraphRelation.object_entity_id == entity.id)
        ).delete(synchronize_session=False)
        db.delete(entity)
        db.flush()

        record = _write_and_project(db, new, thread_id)
        relation = _relation_for(db, record.id)
        assert relation.subject_entity_id != entity.id
        assert (
            db.query(GraphEntity)
            .filter(GraphEntity.normalized_name == "aiive 项目")
            .count()
            == 1
        )


# ---------------------------------------------------------------------------
# 5. 待确认隔离
# ---------------------------------------------------------------------------

class TestPendingIsolation:
    def _defer_plan(self, new: MemoryProposal, old: MemoryRecord) -> ProposalConflictPlan:
        plan = ProposalConflictPlan(proposal_id=new.proposal_id)
        plan.action = ACTION_DEFER
        plan.adjudication = ConflictAdjudication(
            verdict=VERDICT_UNCERTAIN, reason="测试：需要确认", action=ACTION_DEFER,
        )
        plan.old_memory_id = old.id
        plan.old_record_version = old.record_version
        plan.old_content = old.content
        return plan

    def test_pending_keeps_binding_but_writes_no_graph(self, db):
        thread_id, old, entity = _seed_aiive(db)
        new = _proposal(
            "user.employer", "用户在 AIive 项目工作",
            facts=[_fact("用户", "works_at", "AIive 项目", "用户在 AIive 项目工作")],
        )
        discovery = _discover(db, new, thread_id)
        assert "AIive 项目" in discovery.pending_entity_names
        MemoryConflictService.decide(
            [new], {new.proposal_id: discovery},
            llm_client=ScriptedLLM(
                _binding_response("AIive 项目", BIND_REUSE, entity.id, verdict="uncertain")
            ),
        )
        assert new.graph_facts[0].object_entity_id == entity.id

        turn = TurnRecord(
            thread_id=thread_id, turn_id=str(uuid.uuid4()), turn_sequence=1,
            status="running",
        )
        db.add(turn)
        db.flush()
        run = MemoryIngestionRun(
            source_turn_record_id=turn.id, extractor_name="test",
            extractor_version="1.0", status="running",
        )
        db.add(run)
        db.flush()

        before_relations = db.query(GraphRelation).count()
        before_entities = db.query(GraphEntity).count()

        results = MemoryWriteService(db).write_batch(
            proposals=[new], ingestion_run=run, run_context=_ctx(thread_id),
            conflict_plan={new.proposal_id: self._defer_plan(new, old)},
        )
        db.commit()

        assert results[0].outcome is WriteOutcome.CONFLICT_PENDING
        assert db.query(GraphRelation).count() == before_relations
        assert db.query(GraphEntity).count() == before_entities
        assert db.query(MemoryRecord).count() == 1

        case = db.query(MemoryConflictCase).one()
        snapshot = case.proposal_payload["graph_facts"][0]
        assert snapshot["object_entity_id"] == entity.id

        # 用户确认后：走正式写入 + 投影，绑定被重新校验并复用
        outcome = MemoryConflictService.apply_decision(
            db, case_id=case.id,
            decision=ReplyDecision(decision=DECISION_ACCEPT_NEW, reason="以新的为准"),
            run_context=_ctx(thread_id),
        )
        db.commit()
        assert outcome.status == "resolved"
        record = db.get(MemoryRecord, outcome.memory_id)
        GraphProjectionService(db).refresh(record.id, record.record_version)
        db.commit()

        relation = _relation_for(db, record.id)
        assert relation.object_entity_id == entity.id
        assert (
            db.query(GraphEntity)
            .filter(GraphEntity.normalized_name == "aiive 项目")
            .count()
            == 0
        )


# ---------------------------------------------------------------------------
# 6. 投影幂等 / 7. 一次调用
# ---------------------------------------------------------------------------

class TestProjectionAndCallReuse:
    def test_projection_is_idempotent_with_binding(self, db):
        thread_id, _old, entity = _seed_aiive(db)
        new = _proposal(
            "project.tech_stack", "AIive 项目使用 Python",
            facts=[_fact("AIive 项目", "uses_language", "Python", "AIive 项目使用 Python")],
        )
        discovery = _discover(db, new, thread_id)
        MemoryConflictService.decide(
            [new], {new.proposal_id: discovery},
            llm_client=ScriptedLLM(_binding_response("AIive 项目", BIND_REUSE, entity.id)),
        )
        record = _write_and_project(db, new, thread_id)
        first = _relation_for(db, record.id)

        GraphProjectionService(db).refresh(record.id, record.record_version)
        GraphProjectionService(db).refresh(record.id, record.record_version)
        db.commit()

        relations = (
            db.query(GraphRelation)
            .filter(GraphRelation.source_memory_id == record.id)
            .all()
        )
        assert len(relations) == 1
        assert relations[0].id == first.id
        assert relations[0].subject_entity_id == entity.id
        assert (
            db.query(GraphEntity)
            .filter(GraphEntity.normalized_name == "aiive 项目")
            .count()
            == 0
        )

    def test_conflict_and_binding_share_one_llm_call(self, db):
        thread_id, old, entity = _seed_aiive(db)
        new = _proposal(
            "user.employer", "用户在 AIive 项目工作",
            facts=[_fact("用户", "works_at", "AIive 项目", "用户在 AIive 项目工作")],
        )
        discovery = _discover(db, new, thread_id)
        assert discovery.candidates, "同键应产生冲突候选"

        fake = ScriptedLLM(
            _binding_response("AIive 项目", BIND_REUSE, entity.id, verdict=VERDICT_COMPATIBLE)
        )
        plans = MemoryConflictService.decide(
            [new], {new.proposal_id: discovery}, llm_client=fake,
        )

        assert fake.calls == 1
        assert plans[new.proposal_id].adjudication.verdict == VERDICT_COMPATIBLE
        assert plans[new.proposal_id].action == ACTION_PROCEED
        assert new.graph_facts[0].object_entity_id == entity.id
        assert old.id

# ---------------------------------------------------------------------------
# 8. 重建保留绑定语义
# ---------------------------------------------------------------------------

class TestRebuildKeepsBinding:
    def test_rebuild_keeps_bound_entity(self, db):
        from aiive.memory.graph_rebuild import GraphRebuildService

        thread_id, _old, entity = _seed_aiive(db)
        new = _proposal(
            "project.tech_stack", "AIive 项目使用 Python",
            facts=[_fact("AIive 项目", "uses_language", "Python", "AIive 项目使用 Python")],
        )
        discovery = _discover(db, new, thread_id)
        MemoryConflictService.decide(
            [new], {new.proposal_id: discovery},
            llm_client=ScriptedLLM(_binding_response("AIive 项目", BIND_REUSE, entity.id)),
        )
        record = _write_and_project(db, new, thread_id)
        before = _relation_for(db, record.id)
        assert before.subject_entity_id == entity.id

        # 重建从 canonical 记录重新投影：绑定随 graph_facts 一起保留。
        GraphRebuildService(db).rebuild(batch_size=50, repair=False)
        db.commit()

        after = _relation_for(db, record.id)
        assert after.subject_entity_id == entity.id
        assert (
            db.query(GraphEntity)
            .filter(GraphEntity.normalized_name == "aiive 项目")
            .count()
            == 0
        )


# ---------------------------------------------------------------------------
# 9. 名称歧义（同名多实体 / 同一名称多次出现）
# ---------------------------------------------------------------------------

def _mercury_entities(db) -> tuple[GraphEntity, GraphEntity]:
    """同 Scope 下两个不同实体共用别名 Mercury。"""
    planet = GraphEntity(
        normalized_name="水星", display_name="水星", entity_type="celestial",
        aliases=["Mercury"], scope_type="global", scope_id=None,
    )
    mission = GraphEntity(
        normalized_name="信使计划", display_name="信使计划", entity_type="project",
        aliases=["Mercury"], scope_type="global", scope_id=None,
    )
    db.add(planet)
    db.add(mission)
    db.flush()
    return planet, mission


class TestNameAmbiguity:
    def test_shared_alias_never_picks_one_entity(self, db):
        thread_id = _new_thread(db)
        planet, mission = _mercury_entities(db)
        new = _proposal(
            "project.mercury", "Mercury 是一颗行星",
            facts=[_fact("Mercury", "is_planet", "planet", "Mercury 是一颗行星")],
        )

        discovery = _discover(db, new, thread_id)

        # 两个实体都带这个别名 → 不做确定性绑定，两个都进候选
        assert discovery.resolved_entity_bindings == {}
        assert "Mercury" in discovery.pending_entity_names
        assert "entity_name_ambiguous:mercury" in discovery.notes
        assert {planet.id, mission.id} <= {
            item.entity_id for item in discovery.entity_candidates
        }

        plans = MemoryConflictService.decide(
            [new], {new.proposal_id: discovery},
            llm_client=ScriptedLLM(_binding_response("Mercury", BIND_UNRESOLVED)),
        )

        assert plans[new.proposal_id].entity_bindings[0]["accepted"] is False
        assert new.graph_facts[0].subject_entity_id is None

        record = _write_and_project(db, new, thread_id)
        relation = _relation_for(db, record.id)
        assert relation.subject_entity_id not in (planet.id, mission.id)
        assert (
            db.query(GraphEntity)
            .filter(GraphEntity.normalized_name == "mercury")
            .count()
            == 1
        )

    def test_same_name_in_two_facts_binds_separately(self, db):
        thread_id = _new_thread(db)
        planet, mission = _mercury_entities(db)
        new = _proposal(
            "project.mercury", "水星是行星；信使计划代号也是 Mercury",
            facts=[
                _fact("Mercury", "is_planet", "planet", "水星是行星"),
                _fact("Mercury", "is_codename", "mission", "信使计划代号也是 Mercury"),
            ],
        )
        discovery = _discover(db, new, thread_id)
        assert "Mercury" in discovery.pending_entity_names

        fake = ScriptedLLM(json.dumps({
            "verdict": "compatible",
            "reason": "两条事实指向不同实体",
            "confidence": 0.8,
            "entity_bindings": [
                {"name": "Mercury", "decision": "reuse", "entity_id": planet.id,
                 "fact_index": 0, "role": "subject", "confidence": 0.9},
                {"name": "Mercury", "decision": "reuse", "entity_id": mission.id,
                 "fact_index": 1, "role": "subject", "confidence": 0.9},
            ],
        }, ensure_ascii=False))
        MemoryConflictService.decide(
            [new], {new.proposal_id: discovery}, llm_client=fake,
        )

        assert new.graph_facts[0].subject_entity_id == planet.id
        assert new.graph_facts[1].subject_entity_id == mission.id

        record = _write_and_project(db, new, thread_id)
        relations = (
            db.query(GraphRelation)
            .filter(GraphRelation.source_memory_id == record.id)
            .all()
        )
        assert {item.subject_entity_id for item in relations} == {planet.id, mission.id}

    def test_contradictory_duplicates_are_order_independent(self, db):
        thread_id = _new_thread(db)
        planet, mission = _mercury_entities(db)
        entries = [
            {"name": "Mercury", "decision": "reuse", "entity_id": planet.id,
             "fact_index": 0, "role": "subject", "confidence": 0.9},
            {"name": "Mercury", "decision": "reuse", "entity_id": mission.id,
             "fact_index": 0, "role": "subject", "confidence": 0.9},
        ]

        outcomes = []
        for order in (entries, list(reversed(entries))):
            proposal = _proposal(
                "project.mercury", "Mercury 是一颗行星",
                facts=[_fact("Mercury", "is_planet", "planet", "Mercury 是一颗行星")],
            )
            discovery = _discover(db, proposal, thread_id)
            MemoryConflictService.decide(
                [proposal], {proposal.proposal_id: discovery},
                llm_client=ScriptedLLM(json.dumps({
                    "verdict": "compatible", "reason": "测试", "confidence": 0.8,
                    "entity_bindings": order,
                }, ensure_ascii=False)),
            )
            outcomes.append(proposal.graph_facts[0].subject_entity_id)

        assert outcomes == [None, None]
