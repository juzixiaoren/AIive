"""长期记忆冲突处理闭环：候选发现 → 语义裁决 → 待确认 → 用户回复 → 正式写入。

全部离线：SQLite 共享库 + 脚本化伪 LLM，不访问网络、不需要 API Key。
"""
from __future__ import annotations

import types
import uuid
from datetime import UTC, datetime, timedelta

from aiive.context.run_context import RunContext
from aiive.db.models import (
    Event,
    GraphRelation,
    MemoryConflictCase,
    MemoryIngestionRun,
    MemoryRecord,
    OutboxJob,
    Thread,
    TurnRecord,
)
from aiive.memory.conflict_adjudication import (
    ACTION_DEFER,
    ACTION_PROCEED,
    DECISION_ACCEPT_NEW,
    DECISION_FORGET_OLD,
    DECISION_KEEP_BOTH,
    DECISION_KEEP_OLD,
    DECISION_UNRELATED,
    VERDICT_UNCERTAIN,
    ConflictAdjudication,
    ReplyDecision,
)
from aiive.memory.conflict_candidates import (
    SOURCE_EXACT,
    SOURCE_GRAPH,
    SOURCE_RETRIEVAL,
    ConflictCandidateFinder,
)
from aiive.memory.conflict_confirmation import (
    JOB_CONFLICT_CLEANUP,
    JOB_CONFLICT_QUESTION,
    JOB_CONFLICT_REPLY,
    STATUS_ASKING,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_RESOLVED,
    STATUS_STALE,
    MemoryConflictService,
    ProposalConflictPlan,
)
from aiive.memory.graph_expansion import GraphExpansionService
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
from aiive.worker import handlers_conflict

from tests.integration.backend._util import claim_job

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

class ScriptedLLM:
    """按调用顺序返回预设内容的伪 LLM（签名兼容 LLMClient.chat）。"""

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses) or ['{"verdict": "uncertain"}']
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


def _new_thread(db, title: str = "冲突线程") -> str:
    thread_id = str(uuid.uuid4())
    db.add(Thread(id=thread_id, title=title))
    db.flush()
    return thread_id


def _ctx(thread_id: str) -> RunContext:
    return RunContext(
        thread_id=thread_id, trace_id="trace-conflict", source="test",
        execution_mode="user_required",
    )


def _proposal(
    key: str,
    content: str,
    *,
    facts: list[GraphFact] | None = None,
    scope: tuple[str, str | None] = ("global", None),
    memory_type: str = "user_profile",
    confidence: float = 0.9,
    trust: str = TrustLevel.TRUSTED.value,
) -> MemoryProposal:
    proposal = MemoryProposal(
        memory_type=memory_type,
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
        confidence=confidence,
        trust_level=trust,
    )
    proposal.compute_content_hash()
    proposal.compute_request_idempotency()
    return proposal


def _fact(subject: str, predicate: str, object_: str, span: str) -> GraphFact:
    return GraphFact(
        subject=subject, predicate=predicate, object=object_,
        confidence=0.9, source_span=span,
    )


def _seed_record(db, proposal: MemoryProposal) -> MemoryRecord:
    """直接落一条 active+valid 记忆并投影图谱（模拟"既有记忆"）。"""
    record = MemoryStore(db).create_record(
        proposal, lifecycle_state=LifecycleState.ACTIVE.value,
        validity_state=ValidityState.VALID.value,
    )
    db.flush()
    GraphProjectionService(db).refresh(record.id, record.record_version)
    db.flush()
    return record


def _relations(db, memory_id: str) -> list[GraphRelation]:
    return (
        db.query(GraphRelation)
        .filter(GraphRelation.source_memory_id == memory_id)
        .order_by(GraphRelation.recorded_at.asc())
        .all()
    )


def _defer_plan(proposal: MemoryProposal, old: MemoryRecord, verdict: str = VERDICT_UNCERTAIN) -> ProposalConflictPlan:
    plan = ProposalConflictPlan(proposal_id=proposal.proposal_id)
    plan.action = ACTION_DEFER
    plan.adjudication = ConflictAdjudication(
        verdict=verdict, reason="测试：无法可靠裁决", action=ACTION_DEFER,
    )
    plan.old_memory_id = old.id
    plan.old_record_version = old.record_version
    plan.old_content = old.content
    return plan


def _park_case(db, *, thread_id: str, proposal: MemoryProposal, old: MemoryRecord,
               verdict: str = VERDICT_UNCERTAIN) -> MemoryConflictCase:
    case_id = MemoryConflictService.park(
        db, proposal=proposal, plan=_defer_plan(proposal, old, verdict),
        run_context=_ctx(thread_id),
    )
    db.commit()
    return db.get(MemoryConflictCase, case_id)


def _claim_question_job(db, case_id: str):
    job = (
        db.query(OutboxJob)
        .filter(OutboxJob.job_type == JOB_CONFLICT_QUESTION)
        .order_by(OutboxJob.created_at.desc())
        .first()
    )
    assert job is not None
    return claim_job(db, job)


# ---------------------------------------------------------------------------
# 候选发现
# ---------------------------------------------------------------------------

class TestCandidateDiscovery:
    def test_exact_key_candidate_is_found(self, db):
        _new_thread(db)
        old = _seed_record(db, _proposal("user.preference.coffee", "用户喜欢喝咖啡"))
        new = _proposal("user.preference.coffee", "用户不喜欢喝咖啡")

        discovery = ConflictCandidateFinder(db).discover(
            new, scope_chain=[("global", None)], scope_context=None,
        )

        assert discovery.exact_conflict is True
        assert [item.memory_id for item in discovery.candidates] == [old.id]
        assert SOURCE_EXACT in discovery.candidates[0].sources

    def test_graph_relation_supplies_cross_key_candidate(self, db):
        """键不同但图谱关系同主体同谓词 → 必须发现冲突（不能只看同键）。"""
        thread_id = _new_thread(db)
        old = _seed_record(db, _proposal(
            "user.lives_in", "用户住在北京",
            facts=[_fact("用户", "lives_in", "北京", "用户住在北京")],
        ))
        new = _proposal(
            "user.profile.city", "用户现在住在上海",
            facts=[_fact("用户", "lives_in", "上海", "用户现在住在上海")],
        )

        discovery = ConflictCandidateFinder(db).discover(
            new, scope_chain=[("thread", thread_id), ("global", None)], scope_context=None,
        )

        assert discovery.exact_conflict is False
        assert old.id in [item.memory_id for item in discovery.candidates]
        candidate = next(item for item in discovery.candidates if item.memory_id == old.id)
        assert SOURCE_GRAPH in candidate.sources
        assert candidate.graph_relations
        assert any(
            item["object_differs"] and item["cardinality"] == "single"
            for item in discovery.graph_evidence
        )

    def test_identical_content_is_not_a_conflict_candidate(self, db):
        _seed_record(db, _proposal("user.preference.coffee", "用户喜欢喝咖啡"))
        same = _proposal("user.preference.coffee", "用户喜欢喝咖啡")
        same.compute_content_hash()

        discovery = ConflictCandidateFinder(db).discover(
            same, scope_chain=[("global", None)], scope_context=None,
        )

        assert discovery.candidates == []
        assert any(note.startswith("candidate_identical") for note in discovery.notes)


# ---------------------------------------------------------------------------
# 待确认：不污染记忆与图谱
# ---------------------------------------------------------------------------

class TestPendingConfirmation:
    def test_write_batch_defers_without_writing_memory_or_relations(self, db):
        thread_id = _new_thread(db)
        old = _seed_record(db, _proposal(
            "user.lives_in", "用户住在北京",
            facts=[_fact("用户", "lives_in", "北京", "用户住在北京")],
        ))
        old_relations = _relations(db, old.id)
        assert old_relations and old_relations[0].status == "active"

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

        new = _proposal("user.lives_in", "用户现在住在上海")
        writer = MemoryWriteService(db)
        results = writer.write_batch(
            proposals=[new], ingestion_run=run, run_context=_ctx(thread_id),
            conflict_plan={new.proposal_id: _defer_plan(new, old)},
        )
        db.commit()

        assert results[0].outcome is WriteOutcome.CONFLICT_PENDING
        assert results[0].written is False

        # 旧记忆与旧关系保持不变；没有产生新记忆、新关系。
        old_after = db.get(MemoryRecord, old.id)
        assert old_after.validity_state == ValidityState.VALID.value
        assert old_after.record_version == old.record_version
        assert db.query(MemoryRecord).count() == 1
        assert db.query(GraphRelation).count() == len(old_relations)
        assert _relations(db, old.id)[0].status == "active"

        case = db.query(MemoryConflictCase).one()
        assert case.status == STATUS_PENDING
        assert case.old_memory_id == old.id
        assert case.verdict == VERDICT_UNCERTAIN
        assert db.query(OutboxJob).filter(OutboxJob.job_type == JOB_CONFLICT_QUESTION).count() == 1

    def test_park_is_idempotent_for_duplicate_proposals(self, db):
        thread_id = _new_thread(db)
        old = _seed_record(db, _proposal("user.lives_in", "用户住在北京"))
        new = _proposal("user.lives_in", "用户住在上海")

        first = _park_case(db, thread_id=thread_id, proposal=new, old=old)
        second = _park_case(db, thread_id=thread_id, proposal=new, old=old)

        assert first.id == second.id
        assert db.query(MemoryConflictCase).count() == 1
        assert db.query(OutboxJob).filter(OutboxJob.job_type == JOB_CONFLICT_QUESTION).count() == 1

    def test_question_handler_publishes_notification_and_moves_to_asking(self, db, monkeypatch):
        thread_id = _new_thread(db)
        old = _seed_record(db, _proposal("user.lives_in", "用户住在北京"))
        new = _proposal("user.lives_in", "用户现在住在上海")
        case = _park_case(db, thread_id=thread_id, proposal=new, old=old)

        fake = ScriptedLLM("我记得你住在北京，这次说住在上海。请问以哪个为准：以新的为准 / 保留旧的？")
        monkeypatch.setattr(handlers_conflict, "default_llm_client", lambda: fake)
        claimed = _claim_question_job(db, case.id)

        result = handlers_conflict.handle_memory_conflict_question(claimed)

        assert result.outcome.value == "completed"
        db.expire_all()
        case_after = db.get(MemoryConflictCase, case.id)
        assert case_after.status == STATUS_ASKING
        assert "上海" in case_after.question
        event = (
            db.query(Event)
            .filter(Event.trace_id == case.id, Event.event_type == "notification_created")
            .one()
        )
        assert event.payload["conflict_case_id"] == case.id
        assert event.payload["status"] == "pending"
        assert case_after.question_event_id == event.id


# ---------------------------------------------------------------------------
# 用户回复 → 正式写入
# ---------------------------------------------------------------------------

class TestReplyDecisions:
    def test_accept_new_supersedes_and_closes_single_valued_relation(self, db):
        thread_id = _new_thread(db)
        old = _seed_record(db, _proposal(
            "user.lives_in", "用户住在北京",
            facts=[_fact("用户", "lives_in", "北京", "用户住在北京")],
        ))
        new = _proposal(
            "user.lives_in", "用户现在住在上海",
            facts=[_fact("用户", "lives_in", "上海", "用户现在住在上海")],
        )
        case = _park_case(db, thread_id=thread_id, proposal=new, old=old)

        outcome = MemoryConflictService.apply_decision(
            db, case_id=case.id,
            decision=ReplyDecision(decision=DECISION_ACCEPT_NEW, reason="以新的为准"),
            run_context=_ctx(thread_id),
        )

        assert outcome.status == STATUS_RESOLVED
        assert outcome.wrote is True
        new_record = db.get(MemoryRecord, outcome.memory_id)
        assert new_record is not None
        assert new_record.lifecycle_state == LifecycleState.ACTIVE.value
        old_after = db.get(MemoryRecord, old.id)
        assert old_after.validity_state == ValidityState.SUPERSEDED.value
        assert old_after.superseded_by == new_record.id

        # 图谱只跟随记忆层权威取代：单值谓词旧关系被关闭并留下历史。
        GraphProjectionService(db).refresh(new_record.id, new_record.record_version)
        db.commit()
        old_relation = _relations(db, old.id)[0]
        assert old_relation.status == "closed"
        assert old_relation.valid_to is not None
        assert old_relation.superseded_by_id is not None
        new_relations = _relations(db, new_record.id)
        assert [rel.status for rel in new_relations] == ["active"]

    def test_accept_new_across_different_keys_supersedes_old_memory(self, db):
        """键不同但语义冲突（图谱同主体同谓词）时，仍要能按用户决策取代旧记忆。"""
        thread_id = _new_thread(db)
        old = _seed_record(db, _proposal(
            "user.lives_in", "用户住在北京",
            facts=[_fact("用户", "lives_in", "北京", "用户住在北京")],
        ))
        new = _proposal(
            "user.profile.city", "用户现在住在上海",
            facts=[_fact("用户", "lives_in", "上海", "用户现在住在上海")],
        )
        case = _park_case(db, thread_id=thread_id, proposal=new, old=old)

        outcome = MemoryConflictService.apply_decision(
            db, case_id=case.id,
            decision=ReplyDecision(decision=DECISION_ACCEPT_NEW, reason="以新的为准"),
            run_context=_ctx(thread_id),
        )

        assert outcome.status == STATUS_RESOLVED
        new_record = db.get(MemoryRecord, outcome.memory_id)
        assert new_record.canonical_key == "user.profile.city"
        old_after = db.get(MemoryRecord, old.id)
        assert old_after.validity_state == ValidityState.SUPERSEDED.value
        assert old_after.superseded_by == new_record.id

    def test_keep_old_leaves_memory_and_graph_untouched(self, db):
        thread_id = _new_thread(db)
        old = _seed_record(db, _proposal(
            "user.lives_in", "用户住在北京",
            facts=[_fact("用户", "lives_in", "北京", "用户住在北京")],
        ))
        new = _proposal("user.lives_in", "用户现在住在上海")
        case = _park_case(db, thread_id=thread_id, proposal=new, old=old)

        outcome = MemoryConflictService.apply_decision(
            db, case_id=case.id,
            decision=ReplyDecision(decision=DECISION_KEEP_OLD, reason="保留旧的"),
            run_context=_ctx(thread_id),
        )

        assert outcome.status == STATUS_REJECTED
        assert outcome.wrote is False
        assert db.query(MemoryRecord).count() == 1
        assert _relations(db, old.id)[0].status == "active"
        assert db.get(MemoryRecord, old.id).record_version == old.record_version

    def test_keep_both_on_single_valued_key_without_condition_keeps_asking(self, db):
        thread_id = _new_thread(db)
        old = _seed_record(db, _proposal("user.display_name", "小明"))
        new = _proposal("user.display_name", "大明")
        case = _park_case(db, thread_id=thread_id, proposal=new, old=old)

        outcome = MemoryConflictService.apply_decision(
            db, case_id=case.id,
            decision=ReplyDecision(decision=DECISION_KEEP_BOTH, reason="都留着"),
            run_context=_ctx(thread_id),
        )

        assert outcome.status == STATUS_ASKING
        assert outcome.clarify_rounds == 1
        assert db.query(MemoryRecord).count() == 1
        db.expire_all()
        case_after = db.get(MemoryConflictCase, case.id)
        assert case_after.question == ""
        assert (
            db.query(OutboxJob)
            .filter(OutboxJob.operation_id == f"conflict_question:{case.id}:2")
            .count() == 1
        )

    def test_keep_both_with_condition_is_written(self, db):
        thread_id = _new_thread(db)
        old = _seed_record(db, _proposal("user.display_name", "小明"))
        new = _proposal("user.display_name", "大明")
        case = _park_case(db, thread_id=thread_id, proposal=new, old=old)

        outcome = MemoryConflictService.apply_decision(
            db, case_id=case.id,
            decision=ReplyDecision(
                decision=DECISION_KEEP_BOTH, reason="不同场合", condition="工作场合",
            ),
            run_context=_ctx(thread_id),
        )

        assert outcome.status == STATUS_RESOLVED
        record = db.get(MemoryRecord, outcome.memory_id)
        assert "工作场合" in record.content
        assert db.get(MemoryRecord, old.id).validity_state == ValidityState.VALID.value

    def test_forget_old_uses_forget_saga_and_hides_graph_fact(self, db):
        thread_id = _new_thread(db)
        old = _seed_record(db, _proposal(
            "user.lives_in", "用户住在北京",
            facts=[_fact("用户", "lives_in", "北京", "用户住在北京")],
        ))
        new = _proposal("user.lives_in", "用户现在住在上海")
        case = _park_case(db, thread_id=thread_id, proposal=new, old=old)

        outcome = MemoryConflictService.apply_decision(
            db, case_id=case.id,
            decision=ReplyDecision(decision=DECISION_FORGET_OLD, reason="忘掉北京"),
            run_context=_ctx(thread_id),
        )

        assert outcome.status == STATUS_RESOLVED
        db.expire_all()
        old_after = db.get(MemoryRecord, old.id)
        assert old_after.lifecycle_state == LifecycleState.FORGOTTEN.value
        # Forget Saga 继续由既有 Outbox 流程推进（图谱清理/孤儿实体在 cascade 阶段）
        assert (
            db.query(OutboxJob).filter(OutboxJob.job_type == "forget_cascade").count() >= 1
        )
        # 已遗忘事实不能经图谱扩展重新出现
        expansion = GraphExpansionService(db).expand(
            seed_memory_ids=[old.id], query="北京",
            scope_chain=[("thread", thread_id), ("global", None)],
        )
        assert all(view.source_memory_id != old.id for view in expansion.relations)
        assert outcome.memory_id != old.id

    def test_reply_replay_is_noop(self, db):
        thread_id = _new_thread(db)
        old = _seed_record(db, _proposal("user.lives_in", "用户住在北京"))
        new = _proposal("user.lives_in", "用户现在住在上海")
        case = _park_case(db, thread_id=thread_id, proposal=new, old=old)
        decision = ReplyDecision(decision=DECISION_ACCEPT_NEW, reason="以新的为准")

        first = MemoryConflictService.apply_decision(
            db, case_id=case.id, decision=decision, run_context=_ctx(thread_id),
        )
        second = MemoryConflictService.apply_decision(
            db, case_id=case.id, decision=decision, run_context=_ctx(thread_id),
        )

        assert first.status == STATUS_RESOLVED
        assert second.reason == "case_not_open"
        assert db.query(MemoryRecord).count() == 2

    def test_stale_case_when_old_record_version_changed(self, db):
        thread_id = _new_thread(db)
        old = _seed_record(db, _proposal("user.lives_in", "用户住在北京"))
        new = _proposal("user.lives_in", "用户现在住在上海")
        case = _park_case(db, thread_id=thread_id, proposal=new, old=old)

        old.record_version += 1
        db.flush()

        outcome = MemoryConflictService.apply_decision(
            db, case_id=case.id,
            decision=ReplyDecision(decision=DECISION_ACCEPT_NEW, reason="以新的为准"),
            run_context=_ctx(thread_id),
        )

        assert outcome.status == STATUS_STALE
        assert db.query(MemoryRecord).count() == 1

    def test_expired_case_is_not_applied(self, db):
        thread_id = _new_thread(db)
        old = _seed_record(db, _proposal("user.lives_in", "用户住在北京"))
        new = _proposal("user.lives_in", "用户现在住在上海")
        case = _park_case(db, thread_id=thread_id, proposal=new, old=old)

        case.expires_at = datetime.now(UTC) - timedelta(hours=1)
        db.flush()

        outcome = MemoryConflictService.apply_decision(
            db, case_id=case.id,
            decision=ReplyDecision(decision=DECISION_ACCEPT_NEW, reason="以新的为准"),
            run_context=_ctx(thread_id),
        )

        assert outcome.status == STATUS_EXPIRED
        assert db.query(MemoryRecord).count() == 1

    def test_unrelated_reply_changes_nothing(self, db):
        thread_id = _new_thread(db)
        old = _seed_record(db, _proposal("user.lives_in", "用户住在北京"))
        new = _proposal("user.lives_in", "用户现在住在上海")
        case = _park_case(db, thread_id=thread_id, proposal=new, old=old)

        outcome = MemoryConflictService.apply_decision(
            db, case_id=case.id,
            decision=ReplyDecision(decision=DECISION_UNRELATED, reason="换话题了"),
            run_context=_ctx(thread_id),
        )

        assert outcome.reason == "unrelated_message"
        assert db.query(MemoryRecord).count() == 1
        assert db.get(MemoryConflictCase, case.id).status == STATUS_PENDING


# ---------------------------------------------------------------------------
# 回复入口 / Handler
# ---------------------------------------------------------------------------

class TestReplyEntryPoint:
    def test_enqueue_reply_jobs_is_idempotent_per_turn(self, db):
        thread_id = _new_thread(db)
        old = _seed_record(db, _proposal("user.lives_in", "用户住在北京"))
        new = _proposal("user.lives_in", "用户现在住在上海")
        _park_case(db, thread_id=thread_id, proposal=new, old=old)

        first = MemoryConflictService.enqueue_reply_jobs(
            db, thread_id=thread_id, user_message="以新的为准",
            turn_id="turn-1", source_event_ids=["evt-1"],
        )
        second = MemoryConflictService.enqueue_reply_jobs(
            db, thread_id=thread_id, user_message="以新的为准",
            turn_id="turn-1", source_event_ids=["evt-1"],
        )
        db.commit()

        assert first == 1
        assert second == 0
        assert db.query(OutboxJob).filter(OutboxJob.job_type == JOB_CONFLICT_REPLY).count() == 1

    def test_reply_handler_writes_through_write_service(self, db, monkeypatch):
        thread_id = _new_thread(db)
        old = _seed_record(db, _proposal("user.lives_in", "用户住在北京"))
        new = _proposal("user.lives_in", "用户现在住在上海")
        case = _park_case(db, thread_id=thread_id, proposal=new, old=old)

        MemoryConflictService.enqueue_reply_jobs(
            db, thread_id=thread_id, user_message="以新的为准",
            turn_id="turn-1", source_event_ids=["evt-1"],
        )
        db.commit()
        job = db.query(OutboxJob).filter(OutboxJob.job_type == JOB_CONFLICT_REPLY).one()
        claimed = claim_job(db, job)

        fake = ScriptedLLM('{"decision": "accept_new", "reason": "用户选择新记忆", "confidence": 0.9}')
        monkeypatch.setattr(handlers_conflict, "default_llm_client", lambda: fake)
        result = handlers_conflict.handle_memory_conflict_reply(claimed)

        assert result.outcome.value == "completed"
        db.expire_all()
        # 只有一条记忆仍然"当前有效"：旧记录 lifecycle 仍是 active，但 validity 已失效
        assert db.query(MemoryRecord).filter(
            MemoryRecord.validity_state == ValidityState.VALID.value,
        ).count() == 1
        assert db.get(MemoryRecord, old.id).validity_state == ValidityState.SUPERSEDED.value
        assert db.get(MemoryConflictCase, case.id).status == STATUS_RESOLVED
        # 提案审计行同步为最终结果（同一幂等键，不新增行）
        assert db.query(MemoryConflictCase).count() == 1


# ---------------------------------------------------------------------------
# 写入前评估（候选发现 + 语义裁决两阶段）
# ---------------------------------------------------------------------------

class TestAssessment:
    def test_unified_retrieval_supplies_semantic_candidate(self, db):
        """键完全不同但语义相近 → 统一检索必须贡献候选（不能只看同键）。"""
        _seed_record(db, _proposal(
            "user.preference.coffee", "用户每天早上都要喝手冲咖啡",
        ))
        new = _proposal(
            "user.habit.morning_drink", "用户每天早上都要喝手冲咖啡再配一块方糖",
        )

        discovery = ConflictCandidateFinder(db).discover(
            new, scope_chain=[("global", None)], scope_context=None,
        )

        assert discovery.candidates, "统一检索未贡献任何候选"
        assert any(
            SOURCE_RETRIEVAL in item.sources for item in discovery.candidates
        )

    def test_assess_proposals_defers_on_uncertain_verdict(self, db):
        thread_id = _new_thread(db)
        _seed_record(db, _proposal(
            "user.lives_in", "用户住在北京",
            facts=[_fact("用户", "lives_in", "北京", "用户住在北京")],
        ))
        new = _proposal(
            "user.profile.city", "用户现在住在上海",
            facts=[_fact("用户", "lives_in", "上海", "用户现在住在上海")],
        )
        llm = ScriptedLLM('{"verdict": "uncertain", "reason": "两地都可能是当前居住地", "question": "你现在住哪？", "confidence": 0.4}')

        plans = MemoryConflictService.assess_proposals(
            db, [new],
            scope_context=ScopeContext(thread_id=thread_id),
            llm_client=llm, trace_id="trace-assess",
        )

        plan = plans[new.proposal_id]
        assert plan.action == ACTION_DEFER
        assert plan.old_memory_id
        assert plan.graph_evidence

    def test_assess_proposals_proceeds_when_complementary(self, db):
        thread_id = _new_thread(db)
        _seed_record(db, _proposal(
            "user.lives_in", "用户住在北京",
            facts=[_fact("用户", "lives_in", "北京", "用户住在北京")],
        ))
        new = _proposal(
            "user.profile.city", "用户现在住在上海",
            facts=[_fact("用户", "lives_in", "上海", "用户现在住在上海")],
        )
        llm = ScriptedLLM('{"verdict": "complementary", "reason": "补充了新的居住信息", "confidence": 0.8}')

        plans = MemoryConflictService.assess_proposals(
            db, [new],
            scope_context=ScopeContext(thread_id=thread_id),
            llm_client=llm, trace_id="trace-assess",
        )

        assert plans[new.proposal_id].action == ACTION_PROCEED



    def test_question_retry_does_not_duplicate_notification(self, db, monkeypatch):
        """同一案例重复执行问题任务只保留一条待处理通知（不刷屏）。"""
        thread_id = _new_thread(db)
        old = _seed_record(db, _proposal("user.lives_in", "用户住在北京"))
        new = _proposal("user.lives_in", "用户现在住在上海")
        case = _park_case(db, thread_id=thread_id, proposal=new, old=old)

        fake = ScriptedLLM("以哪个居住地为准：北京还是上海？")
        monkeypatch.setattr(handlers_conflict, "default_llm_client", lambda: fake)
        claimed = _claim_question_job(db, case.id)

        handlers_conflict.handle_memory_conflict_question(claimed)
        handlers_conflict.handle_memory_conflict_question(claimed)

        events = (
            db.query(Event)
            .filter(Event.trace_id == case.id, Event.event_type == "notification_created")
            .all()
        )
        assert len(events) == 1
        db.expire_all()
        assert db.get(MemoryConflictCase, case.id).question_event_id == events[0].id


# ---------------------------------------------------------------------------
# 过期清扫（TTL）
# ---------------------------------------------------------------------------

class TestExpiryCleanup:
    def _asking_case(self, db, *, key: str, old_content: str, new_content: str):
        thread_id = _new_thread(db)
        old = _seed_record(db, _proposal(key, old_content))
        case = _park_case(
            db, thread_id=thread_id,
            proposal=_proposal(key, new_content), old=old,
        )
        MemoryConflictService.publish_question(db, case.id, None, question="以哪个为准？")
        db.commit()
        return db.get(MemoryConflictCase, case.id), old

    @staticmethod
    def _age(db, case, minutes: int = 5) -> None:
        case.expires_at = datetime.now(UTC) - timedelta(minutes=minutes)
        db.commit()

    def test_expire_marks_expired_and_cancels_notification(self, db):
        case, old = self._asking_case(
            db, key="user.lives_in", old_content="用户住在北京", new_content="用户现在住在上海",
        )
        assert case.status == STATUS_ASKING
        self._age(db, case)

        result = MemoryConflictService.expire_open_cases(db)
        db.commit()

        refreshed = db.get(MemoryConflictCase, case.id)
        assert result["expired"] == 1
        assert refreshed.status == STATUS_EXPIRED
        assert refreshed.resolution_reason == "ttl_expired"
        assert refreshed.resolved_at is not None
        event = (
            db.query(Event)
            .filter(Event.trace_id == case.id, Event.event_type == "notification_created")
            .one()
        )
        assert event.payload["status"] == "cancelled"
        assert event.payload["resolution"] == STATUS_EXPIRED
        # 旧记忆与其关系原样
        assert db.get(MemoryRecord, old.id).validity_state == ValidityState.VALID.value

    def test_expire_is_idempotent(self, db):
        case, _ = self._asking_case(
            db, key="user.lives_in", old_content="用户住在北京", new_content="用户现在住在上海",
        )
        self._age(db, case)

        first = MemoryConflictService.expire_open_cases(db)
        db.commit()
        second = MemoryConflictService.expire_open_cases(db)
        db.commit()

        assert first["expired"] == 1
        assert second["expired"] == 0

    def test_expire_skips_unexpired_case(self, db):
        case, _ = self._asking_case(
            db, key="user.lives_in", old_content="用户住在北京", new_content="用户现在住在上海",
        )

        result = MemoryConflictService.expire_open_cases(db)
        db.commit()

        assert result["expired"] == 0
        assert db.get(MemoryConflictCase, case.id).status == STATUS_ASKING

    def test_expire_does_not_touch_memory_or_graph(self, db):
        case, _ = self._asking_case(
            db, key="user.lives_in", old_content="用户住在北京", new_content="用户现在住在上海",
        )
        before_records = db.query(MemoryRecord).count()
        before_relations = db.query(GraphRelation).count()
        self._age(db, case)

        MemoryConflictService.expire_open_cases(db)
        db.commit()

        assert db.query(MemoryRecord).count() == before_records
        assert db.query(GraphRelation).count() == before_relations

    def _cleanup_job(self, db, *, case_id: str, batch_size: int):
        job = OutboxJob(
            operation_id=f"conflict_expiry:test:{uuid.uuid4()}",
            job_type=JOB_CONFLICT_CLEANUP,
            status="pending",
            payload={"schema_version": 1, "batch_size": batch_size},
            trace_id=case_id,
        )
        db.add(job)
        db.flush()
        return claim_job(db, job)

    def test_cleanup_handler_expires_and_completes(self, db):
        case, _ = self._asking_case(
            db, key="user.lives_in", old_content="用户住在北京", new_content="用户现在住在上海",
        )
        self._age(db, case)
        claimed = self._cleanup_job(db, case_id=case.id, batch_size=50)

        result = handlers_conflict.handle_memory_conflict_cleanup(claimed)

        assert result.outcome.value == "completed"
        assert "expired=1" in result.reason
        assert db.get(MemoryConflictCase, case.id).status == STATUS_EXPIRED

    def test_cleanup_handler_continues_when_batch_full(self, db):
        first, _ = self._asking_case(
            db, key="user.lives_in", old_content="用户住在北京", new_content="用户现在住在上海",
        )
        second, _ = self._asking_case(
            db, key="user.works_at", old_content="用户在腾讯工作", new_content="用户在字节工作",
        )
        self._age(db, first)
        self._age(db, second)
        claimed = self._cleanup_job(db, case_id=first.id, batch_size=1)

        result = handlers_conflict.handle_memory_conflict_cleanup(claimed)

        assert result.outcome.value == "continue"
        expired = (
            db.query(MemoryConflictCase)
            .filter(MemoryConflictCase.status == STATUS_EXPIRED)
            .count()
        )
        assert expired == 1

    def test_cleanup_job_type_is_enabled_and_registered(self):
        from aiive.memory.recall_config import ENABLED_OUTBOX_JOB_TYPES
        from aiive.worker.handler_registry import HandlerRegistry
        from aiive.worker.outbox_handlers import register_all

        assert JOB_CONFLICT_CLEANUP in ENABLED_OUTBOX_JOB_TYPES
        registry = HandlerRegistry()
        register_all(registry)
        assert registry.get(JOB_CONFLICT_CLEANUP) is not None

    def test_scheduler_enqueue_is_window_idempotent(self, db, monkeypatch):
        from aiive.worker import scheduler_daemon

        class _FakeWorker:
            def __init__(self) -> None:
                self.calls = 0

            def enqueue(self, session, job_type, payload, operation_id=None):
                self.calls += 1
                session.add(OutboxJob(
                    operation_id=operation_id, job_type=job_type,
                    status="pending", payload=payload,
                ))
                session.flush()

        fake = _FakeWorker()
        monkeypatch.setattr(scheduler_daemon, "_outbox_worker", fake)
        now = datetime(2026, 10, 25, 4, 30, tzinfo=UTC)

        first = scheduler_daemon.enqueue_conflict_cleanup_job(db, now=now)
        second = scheduler_daemon.enqueue_conflict_cleanup_job(db, now=now)

        assert first == second == "conflict_expiry:2026-10-25T04"
        assert fake.calls == 1
        assert (
            db.query(OutboxJob).filter(OutboxJob.job_type == JOB_CONFLICT_CLEANUP).count() == 1
        )
