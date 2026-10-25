"""长期记忆冲突处理闭环：候选发现 → 语义裁决 → 待确认 → 用户回复 → 正式写入。

职责边界（重要）：
- 本模块**不直接写 memory_records / graph_relations**：所有正式修改都委托给
  MemoryWriteService（Gate + ConflictResolver + 事务 + Outbox 投影）或它封装的
  Forget Saga；图谱关系只跟随 canonical 记录经 GraphProjectionService 投影。
- 待确认期间（pending / asking）不产生记忆记录，也不产生关系行：现有记忆与
  当前有效关系保持原样，新提案只作为案例快照存在。
- LLM 调用（语义裁决 / 澄清问题 / 回复解析）只产出结构化结论，不触碰数据库。

状态机（memory_conflict_cases.status）：
  pending --(问题已生成)--> asking --(用户回复)--> resolved / rejected
                                  |--(旧记忆版本变化)--> stale
                                  |--(超时未答)--> expired
  多轮澄清：clarify 决策把 asking 保持在 asking，只递增 clarify_rounds。
"""
from __future__ import annotations

import logging
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aiive.config import settings
from aiive.context.run_context import RunContext
from aiive.db.models import (
    Event,
    MemoryConflictCase,
    MemoryRecord,
    OutboxJob,
    Thread,
)
from aiive.db.models import (
    MemoryProposal as MemoryProposalModel,
)
from aiive.memory.conflict_adjudication import (
    ACTION_DEFER,
    ACTION_PROCEED,
    BIND_REUSE,
    DECISION_ACCEPT_NEW,
    DECISION_CLARIFY,
    DECISION_FORGET_OLD,
    DECISION_KEEP_BOTH,
    DECISION_KEEP_OLD,
    DECISION_MERGE,
    DECISION_REJECT_NEW,
    DECISION_REVISE,
    DECISION_UNRELATED,
    ConflictAdjudication,
    ConflictQuestionGenerator,
    ConflictReplyParser,
    EntityBinding,
    ReplyDecision,
    SemanticConflictAdjudicator,
    adjudication_to_plan,
    apply_entity_bindings,
    validate_entity_bindings,
)
from aiive.memory.conflict_candidates import CandidateDiscovery, ConflictCandidate
from aiive.memory.memory_key_registry import get_memory_key_registry
from aiive.memory.memory_types import (
    EvidenceItem,
    LifecycleState,
    MemoryProposal,
    TrustLevel,
)

logger = logging.getLogger(__name__)

JOB_CONFLICT_QUESTION = "memory_conflict_question"
JOB_CONFLICT_REPLY = "memory_conflict_reply"
JOB_CONFLICT_CLEANUP = "memory_conflict_cleanup"

STATUS_PENDING = "pending"
STATUS_ASKING = "asking"
STATUS_RESOLVED = "resolved"
STATUS_REJECTED = "rejected"
STATUS_STALE = "stale"
STATUS_EXPIRED = "expired"

_OPEN_STATUSES = (STATUS_PENDING, STATUS_ASKING)

# 用户决策 → MemoryWriteService 的强制决策（空字符串表示走普通解析流程）
_FORCED_WRITE_DECISIONS: dict[str, str] = {
    DECISION_ACCEPT_NEW: "accept_new",
    DECISION_REVISE: "revise",
    DECISION_MERGE: "merge",
    DECISION_KEEP_BOTH: "keep_both",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass
class ProposalConflictPlan:
    """单个提案的冲突处理计划（写入前预判结果，事务内据此决定是否落库）。"""

    proposal_id: str
    action: str = ACTION_PROCEED
    adjudication: ConflictAdjudication = field(default_factory=ConflictAdjudication)
    candidates: list[ConflictCandidate] = field(default_factory=list)
    graph_evidence: list[dict[str, Any]] = field(default_factory=list)
    # 实体绑定建议（已通过服务端校验；同时已回填到 proposal.graph_facts）
    entity_bindings: list[dict[str, Any]] = field(default_factory=list)
    old_memory_id: str = ""
    old_record_version: int = 0
    old_content: str = ""


@dataclass
class ReplyOutcome:
    """一次回复处理的结果（供 Handler / API / 测试观测）。"""

    case_id: str
    status: str
    decision: str = ""
    memory_id: str = ""
    operation: str = ""
    reason: str = ""
    clarify_rounds: int = 0
    wrote: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "status": self.status,
            "decision": self.decision,
            "memory_id": self.memory_id,
            "operation": self.operation,
            "reason": self.reason,
            "clarify_rounds": self.clarify_rounds,
            "wrote": self.wrote,
        }


class MemoryConflictService:
    """冲突闭环编排服务（唯一入口，供 Outbox Handler 与 API 复用）。"""

    # ------------------------------------------------------------------
    # 1. 写入前评估（事务外：允许 LLM 调用）
    # ------------------------------------------------------------------

    @staticmethod
    def collect_candidates(
        db: Session,
        proposals: list[MemoryProposal],
        *,
        scope_context: Any,
    ) -> dict[str, CandidateDiscovery]:
        """第一阶段：只做候选发现（纯数据库读，不含 LLM 调用）。"""
        if not settings.aiive_memory_conflict_confirmation_enabled:
            return {}
        from aiive.memory.conflict_candidates import ConflictCandidateFinder

        scope_chain = scope_context.chain() if hasattr(scope_context, "chain") else []
        finder = ConflictCandidateFinder(
            db,
            limit=max(1, int(settings.aiive_memory_conflict_max_candidates)),
            retrieval_min_score=float(settings.aiive_memory_conflict_retrieval_min_score),
        )
        discoveries: dict[str, CandidateDiscovery] = {}
        for proposal in proposals:
            try:
                discoveries[proposal.proposal_id] = finder.discover(
                    proposal, scope_chain=scope_chain, scope_context=scope_context,
                )
            except Exception:
                logger.exception("冲突候选发现失败，降级为沿用既有写入流程")
                discoveries[proposal.proposal_id] = CandidateDiscovery(
                    notes=["candidate_discovery_failed"],
                )
        return discoveries

    @classmethod
    def decide(
        cls,
        proposals: list[MemoryProposal],
        discoveries: dict[str, CandidateDiscovery],
        *,
        llm_client: Any,
        trace_id: str | None = None,
    ) -> dict[str, ProposalConflictPlan]:
        """第二阶段：语义裁决 + 确定性计划（不访问数据库）。"""
        adjudicator = SemanticConflictAdjudicator(llm_client)
        plans: dict[str, ProposalConflictPlan] = {}
        for proposal in proposals:
            plan = ProposalConflictPlan(proposal_id=proposal.proposal_id)
            plans[proposal.proposal_id] = plan
            discovery = discoveries.get(proposal.proposal_id)
            if discovery is None:
                continue
            # 别名命中是此前的裁决结果：确定性复用，不占用本次模型判断。
            resolved = [
                EntityBinding(
                    name=name, decision=BIND_REUSE, entity_id=entity_id,
                    confidence=1.0, reason="alias_match", accepted=True,
                )
                for name, entity_id in discovery.resolved_entity_bindings.items()
            ]
            if resolved:
                apply_entity_bindings(proposal, resolved)
                plan.entity_bindings.extend(item.to_dict() for item in resolved)
            if discovery.is_empty() and not discovery.needs_entity_binding():
                plan.adjudication.notes.extend(discovery.notes)
                continue

            # 一次调用同时产出"记忆冲突裁决"和"实体绑定建议"：
            # 没有记忆冲突但有实体消歧需求时也走这里（同一次 LLM 调用）。
            adjudication = adjudicator.adjudicate(
                proposal=proposal,
                candidates=discovery.candidates,
                graph_evidence=discovery.graph_evidence,
                entity_candidates=discovery.entity_candidates,
                pending_entity_names=discovery.pending_entity_names,
                trace_id=trace_id,
            )
            adjudication.entity_bindings = validate_entity_bindings(
                adjudication.entity_bindings,
                pending_names=discovery.pending_entity_names,
                candidate_ids={
                    item.entity_id for item in discovery.entity_candidates
                },
                relation_ids={
                    str(item.get("relation_id", ""))
                    for item in discovery.graph_evidence
                    if item.get("relation_id")
                },
                fact_names=[
                    (fact.subject, fact.object)
                    for fact in (proposal.graph_facts or [])
                ],
            )
            # 只回填已通过校验的 reuse 绑定；三元组本身不变。
            apply_entity_bindings(proposal, adjudication.entity_bindings)
            plan.entity_bindings.extend(
                item.to_dict() for item in adjudication.entity_bindings
            )
            adjudication.notes.extend(discovery.notes)
            plan.adjudication = adjudication_to_plan(
                adjudication,
                canonical_key=proposal.canonical_key,
                graph_evidence=discovery.graph_evidence,
                proposal_trust=proposal.trust_level,
                candidates=discovery.candidates,
            )
            plan.candidates = discovery.candidates
            plan.graph_evidence = discovery.graph_evidence
            plan.action = plan.adjudication.action
            if plan.action == ACTION_DEFER and not discovery.candidates:
                # 没有可比对的旧记忆时无法向用户提出"以哪条为准"，退回既有流程。
                plan.action = ACTION_PROCEED
                plan.adjudication.notes.append("defer_without_candidate_proceed")
            if plan.action == ACTION_DEFER and discovery.candidates:
                # 以检索分最高（其次置信度最高）的候选作为"旧记忆"主体
                old = max(
                    discovery.candidates,
                    key=lambda item: (item.retrieval_score, item.confidence),
                )
                plan.old_memory_id = old.memory_id
                plan.old_record_version = old.record_version
                plan.old_content = old.content
        return plans

    @classmethod
    def assess_proposals(
        cls,
        db: Session,
        proposals: list[MemoryProposal],
        *,
        scope_context: Any,
        llm_client: Any,
        trace_id: str | None = None,
    ) -> dict[str, ProposalConflictPlan]:
        """便捷入口：候选发现 + 语义裁决（调用方需接受会话在裁决期间仍打开）。

        生产路径请分两段调用 collect_candidates / decide，让 LLM 调用完全
        落在数据库会话之外；本方法供测试与脚本使用。
        """
        discoveries = cls.collect_candidates(db, proposals, scope_context=scope_context)
        if not discoveries:
            return {}
        return cls.decide(
            proposals, discoveries, llm_client=llm_client, trace_id=trace_id,
        )

    # ------------------------------------------------------------------
    # 2. 建案（写入事务内：只写案例 + Outbox，不写记忆）
    # ------------------------------------------------------------------

    @classmethod
    def park(
        cls,
        db: Session,
        *,
        proposal: MemoryProposal,
        plan: ProposalConflictPlan,
        run_context: RunContext,
    ) -> str:
        """持久化待确认案例并入队澄清问题任务；返回案例 id（幂等）。"""
        ikey = cls._case_idempotency_key(proposal, plan.old_memory_id)
        existing = (
            db.query(MemoryConflictCase)
            .filter(MemoryConflictCase.idempotency_key == ikey)
            .one_or_none()
        )
        if existing is not None:
            # 重复提案：不重复建案、不重复通知，沿用已有案例继续走。
            return existing.id

        now = _utcnow()
        case = MemoryConflictCase(
            id=str(_uuid.uuid4()),
            status=STATUS_PENDING,
            thread_id=run_context.thread_id,
            scope_type=proposal.scope_type,
            scope_id=proposal.scope_id,
            canonical_key=proposal.canonical_key,
            memory_type=proposal.memory_type,
            sensitivity=proposal.sensitivity,
            old_memory_id=plan.old_memory_id or None,
            old_record_version=plan.old_record_version,
            old_content=plan.old_content,
            new_content=proposal.content,
            proposal_id=proposal.proposal_id,
            proposal_payload=proposal.model_dump(mode="json"),
            verdict=plan.adjudication.verdict,
            verdict_reason=plan.adjudication.reason,
            evidence=[candidate.to_evidence() for candidate in plan.candidates],
            graph_evidence=list(plan.graph_evidence),
            question="",
            clarify_rounds=0,
            idempotency_key=ikey,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(hours=max(1, int(settings.aiive_memory_conflict_ttl_hours))),
        )
        try:
            with db.begin_nested():
                db.add(case)
                db.flush()
        except IntegrityError:
            # 并发重复提案：唯一键兜底，回读已存在案例。
            db.expunge(case) if case in db else None
            existing = (
                db.query(MemoryConflictCase)
                .filter(MemoryConflictCase.idempotency_key == ikey)
                .one_or_none()
            )
            if existing is not None:
                return existing.id
            raise

        cls._enqueue_question_job(db, case, round_no=1)
        logger.info(
            "记忆冲突待确认：case=%s key=%s old=%s verdict=%s",
            case.id, case.canonical_key, case.old_memory_id, case.verdict,
        )
        return case.id

    @staticmethod
    def _case_idempotency_key(proposal: MemoryProposal, old_memory_id: str) -> str:
        base = proposal.idempotency_key or proposal.proposal_id
        return f"conflict:{base}:{old_memory_id or 'none'}"[:128]

    @staticmethod
    def _enqueue_question_job(db: Session, case: MemoryConflictCase, *, round_no: int) -> None:
        """入队澄清问题生成任务（同案例同轮次幂等）。"""
        op_id = f"conflict_question:{case.id}:{round_no}"
        if db.query(OutboxJob).filter(OutboxJob.operation_id == op_id).first() is not None:
            return
        db.add(OutboxJob(
            operation_id=op_id,
            job_type=JOB_CONFLICT_QUESTION,
            status="pending",
            payload={
                "schema_version": 1,
                "conflict_case_id": case.id,
                "round": round_no,
            },
            trace_id=case.id,
            max_retries=3,
        ))

    # ------------------------------------------------------------------
    # 3. 生成澄清问题 + 通知（Outbox Handler 调用，事务外 LLM）
    # ------------------------------------------------------------------

    @classmethod
    def publish_question(
        cls, db: Session, case_id: str, llm_client: Any, question: str = "",
    ) -> dict[str, Any]:
        """为待确认案例生成问题并写入通知事件（幂等、可重复执行）。

        question 非空时直接使用（调用方已在事务外完成 LLM 生成），避免在
        数据库事务里等待模型响应。
        """
        case = cls._load_case_for_update(db, case_id)
        if case is None:
            return {"status": "missing_case"}
        if case.status not in _OPEN_STATUSES:
            return {"status": "not_open", "case_status": case.status}
        if cls._is_expired(case):
            cls._mark_status(case, STATUS_EXPIRED, reason="ttl_expired")
            db.commit()
            return {"status": "expired"}

        generator = ConflictQuestionGenerator(llm_client)
        question = question or case.question or ""
        if not question:
            question = generator.generate(
                old_content=case.old_content,
                new_content=case.new_content,
                reason=case.verdict_reason,
                trace_id=case.id,
            )
        event_id = cls._publish_notification(db, case, question)
        case.question = question
        if event_id:
            case.question_event_id = event_id
        if case.status == STATUS_PENDING:
            case.status = STATUS_ASKING
        case.updated_at = _utcnow()
        db.flush()
        return {
            "status": "published",
            "case_id": case.id,
            "question_event_id": case.question_event_id or "",
            "clarify_rounds": case.clarify_rounds,
        }

    @staticmethod
    def _publish_notification(db: Session, case: MemoryConflictCase, question: str) -> str:
        """写一条 notification_created 事件（复用现有通知读模型与角标广播）。

        幂等：同一案例只保留一条"待处理"通知。Handler 重试或澄清轮次递增时
        更新原通知正文，而不是再插一条，避免用户看到重复待办。
        """
        existing = (
            db.query(Event)
            .filter(
                Event.trace_id == case.id,
                Event.event_type == "notification_created",
            )
            .order_by(Event.created_at.desc())
            .first()
        )
        if existing is not None:
            payload = dict(existing.payload or {})
            if payload.get("status") == "pending":
                if question and payload.get("message") != question:
                    payload["message"] = question
                    payload["content"] = question
                    existing.payload = payload
                    db.flush()
                return existing.id
        if db.get(Thread, case.thread_id) is None:
            logger.warning("冲突通知跳过：thread %s 不存在", case.thread_id)
            return ""
        event = Event(
            id=str(_uuid.uuid4()),
            trace_id=case.id,
            thread_id=case.thread_id,
            event_type="notification_created",
            payload={
                "title": "需要确认一条记忆",
                "content": question,
                "message": question,
                "status": "pending",
                "category": "memory_conflict",
                "conflict_case_id": case.id,
                "canonical_key": case.canonical_key,
            },
        )
        db.add(event)
        db.flush()
        try:
            from aiive.api.routes_notifications import count_pending_notifications
            from aiive.api.ws_manager import ws_manager

            ws_manager.broadcast_notification_sync(count_pending_notifications(db))
        except Exception:
            logger.warning("冲突通知角标广播失败（不影响通知落库）", exc_info=True)
        return event.id

    # ------------------------------------------------------------------
    # 4. 用户回复入口：会话回合把回复转成后台任务
    # ------------------------------------------------------------------

    @staticmethod
    def enqueue_reply_jobs(
        db: Session,
        *,
        thread_id: str,
        user_message: str,
        turn_id: str,
        turn_record_id: str = "",
        source_event_ids: list[str] | None = None,
    ) -> int:
        """若该 Thread 有待确认案例，入队一条回复解析任务（同案例同 Turn 幂等）。

        由 Turn 抢占事务内调用（与用户消息事件同事务提交），保证"用户回复"
        与"待确认案例"的关联不会因为进程重启而丢失。
        """
        if not settings.aiive_memory_conflict_confirmation_enabled:
            return 0
        if not thread_id or not (user_message or "").strip():
            return 0
        cases = (
            db.query(MemoryConflictCase)
            .filter(
                MemoryConflictCase.thread_id == thread_id,
                MemoryConflictCase.status.in_(list(_OPEN_STATUSES)),
            )
            .order_by(MemoryConflictCase.created_at.asc())
            .limit(3)
            .all()
        )
        enqueued = 0
        now = _utcnow()
        for case in cases:
            expires_at = _as_utc(case.expires_at)
            if expires_at is not None and expires_at <= now:
                continue
            op_id = f"conflict_reply:{case.id}:{turn_id}"
            if db.query(OutboxJob).filter(OutboxJob.operation_id == op_id).first() is not None:
                continue
            db.add(OutboxJob(
                operation_id=op_id,
                job_type=JOB_CONFLICT_REPLY,
                status="pending",
                payload={
                    "schema_version": 1,
                    "conflict_case_id": case.id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "turn_record_id": turn_record_id,
                    "user_message": user_message,
                    "source_event_ids": list(source_event_ids or []),
                },
                trace_id=case.id,
                max_retries=2,
            ))
            enqueued += 1
        return enqueued

    # ------------------------------------------------------------------
    # 5. 回复解析 + 正式写入（唯一写入路径）
    # ------------------------------------------------------------------

    @classmethod
    def handle_reply(
        cls,
        db: Session,
        *,
        case_id: str,
        user_message: str,
        run_context: RunContext,
        llm_client: Any,
        source: str = "user_reply",
    ) -> ReplyOutcome:
        """解析用户回复并执行决策；所有写入经 MemoryWriteService / Forget。"""
        case = db.get(MemoryConflictCase, case_id)
        if case is None:
            return ReplyOutcome(case_id=case_id, status="missing_case", reason="案例不存在")
        if case.status not in _OPEN_STATUSES:
            # 回复重放：已处理过的案例不重复执行。
            return ReplyOutcome(
                case_id=case.id, status=case.status, decision=case.decision or "",
                memory_id=case.resolved_memory_id or "", reason="case_not_open",
            )
        if cls._is_expired(case):
            cls._mark_status(case, STATUS_EXPIRED, reason="ttl_expired")
            db.commit()
            return ReplyOutcome(case_id=case.id, status=STATUS_EXPIRED, reason="案例已过期")

        parser = ConflictReplyParser(llm_client)
        decision = parser.parse(
            question=case.question,
            old_content=case.old_content,
            new_content=case.new_content,
            user_reply=user_message,
            trace_id=case.id,
        )
        return cls.apply_decision(
            db, case_id=case.id, decision=decision, run_context=run_context, source=source,
        )

    @classmethod
    def apply_decision(
        cls,
        db: Session,
        *,
        case_id: str,
        decision: ReplyDecision,
        run_context: RunContext,
        source: str = "user_reply",
    ) -> ReplyOutcome:
        """把已解析的结构化决策落到正式写入路径（执行前重新校验版本与状态）。"""
        case = cls._load_case_for_update(db, case_id)
        if case is None:
            return ReplyOutcome(case_id=case_id, status="missing_case", reason="案例不存在")
        if case.status not in _OPEN_STATUSES:
            return ReplyOutcome(
                case_id=case.id, status=case.status, decision=case.decision or "",
                memory_id=case.resolved_memory_id or "", reason="case_not_open",
            )
        if cls._is_expired(case):
            cls._mark_status(case, STATUS_EXPIRED, reason="ttl_expired")
            db.commit()
            return ReplyOutcome(case_id=case.id, status=STATUS_EXPIRED, reason="案例已过期")

        if decision.decision == DECISION_UNRELATED:
            # 与冲突无关的消息：不改变任何记忆，也不消耗澄清轮次。
            return ReplyOutcome(
                case_id=case.id, status=case.status, decision=decision.decision,
                reason="unrelated_message",
            )

        old_record = cls._load_old_record(db, case)
        if decision.decision not in (DECISION_CLARIFY, DECISION_KEEP_OLD, DECISION_REJECT_NEW):
            if old_record is None:
                cls._mark_status(case, STATUS_STALE, decision=decision, reason="old_memory_missing")
                db.commit()
                return ReplyOutcome(
                    case_id=case.id, status=STATUS_STALE, decision=decision.decision,
                    reason="old_memory_missing",
                )
            if int(old_record.record_version or 0) != int(case.old_record_version or 0):
                # 执行前重新检查版本：旧记忆已被别的路径改过，放弃本次自动执行。
                cls._mark_status(case, STATUS_STALE, decision=decision, reason="old_memory_version_changed")
                db.commit()
                return ReplyOutcome(
                    case_id=case.id, status=STATUS_STALE, decision=decision.decision,
                    reason="old_memory_version_changed",
                )
            if (old_record.lifecycle_state or "") != LifecycleState.ACTIVE.value:
                cls._mark_status(case, STATUS_STALE, decision=decision, reason="old_memory_not_active")
                db.commit()
                return ReplyOutcome(
                    case_id=case.id, status=STATUS_STALE, decision=decision.decision,
                    reason="old_memory_not_active",
                )

        if decision.decision == DECISION_CLARIFY:
            return cls._handle_clarify(db, case, decision)
        if decision.decision == DECISION_KEEP_OLD:
            cls._mark_status(
                case, STATUS_REJECTED, decision=decision, reason="user_kept_old",
            )
            db.commit()
            return ReplyOutcome(
                case_id=case.id, status=STATUS_REJECTED, decision=decision.decision,
                reason="user_kept_old", clarify_rounds=case.clarify_rounds,
            )
        if decision.decision == DECISION_REJECT_NEW:
            cls._mark_status(
                case, STATUS_REJECTED, decision=decision, reason="user_rejected_new",
            )
            db.commit()
            return ReplyOutcome(
                case_id=case.id, status=STATUS_REJECTED, decision=decision.decision,
                reason="user_rejected_new", clarify_rounds=case.clarify_rounds,
            )
        if decision.decision == DECISION_KEEP_BOTH and not cls._keep_both_allowed(case, decision):
            # 单值关系上无法明确条件 → 继续询问，绝不伪造一致性。
            return cls._handle_clarify(
                db, case, decision, extra_note="keep_both_needs_condition",
            )

        return cls._execute_decision(
            db, case, decision, run_context, old_record=old_record, source=source,
        )

    @classmethod
    def _execute_decision(
        cls,
        db: Session,
        case: MemoryConflictCase,
        decision: ReplyDecision,
        run_context: RunContext,
        *,
        old_record: MemoryRecord | None,
        source: str,
    ) -> ReplyOutcome:
        from aiive.memory.memory_write_service import MemoryWriteService

        writer = MemoryWriteService(db)
        proposal = cls._rebuild_proposal(case, decision, run_context, old_record)
        if proposal is None:
            cls._mark_status(case, STATUS_STALE, decision=decision, reason="proposal_snapshot_invalid")
            db.commit()
            return ReplyOutcome(
                case_id=case.id, status=STATUS_STALE, decision=decision.decision,
                reason="proposal_snapshot_invalid",
            )

        result = None
        forgot = False
        if decision.decision == DECISION_FORGET_OLD and old_record is not None:
            forget_result = writer.forget(
                memory_id=old_record.id,
                reason="memory_conflict_user_request",
                run_context=run_context,
            )
            forgot = forget_result.written
            if not forgot:
                cls._mark_status(
                    case, STATUS_STALE, decision=decision,
                    reason=f"forget_failed:{forget_result.reason}",
                )
                db.commit()
                return ReplyOutcome(
                    case_id=case.id, status=STATUS_STALE, decision=decision.decision,
                    reason="forget_failed",
                )

        forced = _FORCED_WRITE_DECISIONS.get(decision.decision, "")
        if decision.decision == DECISION_FORGET_OLD:
            # 旧记忆已被 Forget Saga 遗忘：新提案走普通 create 语义即可。
            forced = ""

        result = writer.write(
            proposal,
            run_context=run_context,
            decision=forced,
            # 跨 canonical_key 的语义冲突必须显式指定被取代的旧记忆，
            # 否则 write() 只能按新提案自己的键查找，找不到目标而失败。
            decision_target_id=case.old_memory_id or "",
        )

        if not result.written and result.outcome.value not in ("ignored", "reinforce_skipped"):
            cls._mark_status(
                case, STATUS_STALE, decision=decision,
                reason=f"write_failed:{result.reason}",
            )
            db.commit()
            return ReplyOutcome(
                case_id=case.id, status=STATUS_STALE, decision=decision.decision,
                reason=result.reason,
            )

        cls._mark_status(
            case,
            STATUS_RESOLVED if decision.decision != DECISION_REJECT_NEW else STATUS_REJECTED,
            decision=decision,
            reason=decision.reason or decision.decision,
            memory_id=result.memory_id,
        )
        case.decision_source = source[:32]
        cls._sync_proposal_record(
            db, case, result.memory_id, forced or decision.decision,
            content=proposal.content,
        )
        cls._close_notification(db, case)
        db.commit()
        logger.info(
            "记忆冲突已按用户决策处理：case=%s decision=%s memory=%s forgot_old=%s",
            case.id, decision.decision, result.memory_id, forgot,
        )
        return ReplyOutcome(
            case_id=case.id,
            status=case.status,
            decision=decision.decision,
            memory_id=result.memory_id,
            operation=result.operation,
            reason=result.reason,
            clarify_rounds=case.clarify_rounds,
            wrote=result.written,
        )

    @classmethod
    def _handle_clarify(
        cls,
        db: Session,
        case: MemoryConflictCase,
        decision: ReplyDecision,
        *,
        extra_note: str = "",
    ) -> ReplyOutcome:
        """继续澄清：递增轮次并再入队一次问题生成；超过上限则保守结束。"""
        max_rounds = max(1, int(settings.aiive_memory_conflict_max_clarify_rounds))
        case.clarify_rounds = int(case.clarify_rounds or 0) + 1
        case.decision = decision.decision
        case.decision_span = (decision.reason or extra_note)[:2000]
        case.updated_at = _utcnow()
        if case.clarify_rounds >= max_rounds:
            # 反复无法澄清：保留旧记忆，不写入新事实，也不产生任何关系。
            cls._mark_status(
                case, STATUS_EXPIRED, decision=decision, reason="clarify_rounds_exhausted",
            )
            cls._close_notification(db, case)
            db.commit()
            return ReplyOutcome(
                case_id=case.id, status=STATUS_EXPIRED, decision=decision.decision,
                reason="clarify_rounds_exhausted", clarify_rounds=case.clarify_rounds,
            )
        # 下一轮问题必须重新生成（用户可能补充了新背景），并清空旧问题文本。
        case.question = ""
        case.status = STATUS_ASKING
        cls._enqueue_question_job(db, case, round_no=case.clarify_rounds + 1)
        db.flush()
        db.commit()
        return ReplyOutcome(
            case_id=case.id, status=STATUS_ASKING, decision=decision.decision,
            reason=extra_note or "needs_more_clarification",
            clarify_rounds=case.clarify_rounds,
        )

    @staticmethod
    def _keep_both_allowed(case: MemoryConflictCase, decision: ReplyDecision) -> bool:
        """保留两条事实是否成立。

        用户给出明确适用条件 → 允许（把条件写进内容，不伪造一致性）；
        否则单值键、或图谱里单值谓词出现不同客体 → 互斥且条件不明 → 继续询问；
        多值键且多值谓词 → 两条事实本来就可以并存。
        """
        if (decision.condition or "").strip():
            return True
        spec = get_memory_key_registry().resolve(case.canonical_key)
        key_single = (spec.cardinality if spec is not None else "multi") == "single"
        if key_single:
            return False
        return not any(
            str(item.get("cardinality", "multi")) == "single"
            and item.get("object_differs")
            for item in (case.graph_evidence or [])
        )

    # ------------------------------------------------------------------
    # 6. 提案重建（不修改历史快照，只在内存里构造本次写入的提案）
    # ------------------------------------------------------------------

    @classmethod
    def _rebuild_proposal(
        cls,
        case: MemoryConflictCase,
        decision: ReplyDecision,
        run_context: RunContext,
        old_record: MemoryRecord | None,
    ) -> MemoryProposal | None:
        """从案例快照重建提案；补充"用户回复"这一真实证据。"""
        payload = case.proposal_payload or {}
        try:
            proposal = MemoryProposal.model_validate(payload)
        except Exception:
            logger.exception("冲突案例提案快照无法重建: case=%s", case.id)
            return None

        content = proposal.content
        if decision.decision in (DECISION_REVISE, DECISION_MERGE):
            content = (decision.merged_content or "").strip() or content
        if decision.decision == DECISION_KEEP_BOTH and decision.condition.strip():
            content = f"{content}（适用条件：{decision.condition.strip()}）"
        proposal.content = content
        proposal.compute_content_hash()

        # 用户回复本身是可信证据：补一条 evidence（不伪造 source_event_id）。
        reply_events = list(run_context.source_event_ids or [])
        proposal.evidence = list(proposal.evidence) + [EvidenceItem(
            source_event_id=reply_events[0] if reply_events else None,
            source_type="user_message",
            trust_level=TrustLevel.TRUSTED.value,
            relation="confirms" if decision.decision != DECISION_REJECT_NEW else "corrects",
            content_span=(decision.reason or decision.decision)[:500],
        )]
        # 用户已明确表态 → 权威性足够，不再因置信度阈值降级为 candidate。
        proposal.confidence = max(float(proposal.confidence or 0.0), 0.95)
        proposal.execution_mode = "user_required"
        proposal.source_turn_id = run_context.turn_id
        proposal.source_turn_record_id = run_context.turn_record_id
        if reply_events:
            proposal.source_event_ids = reply_events
        if decision.decision == DECISION_MERGE and old_record is not None:
            # 合并：把旧记忆的关系事实并入新记录，图谱只跟随这一份 canonical 记录。
            merged_facts = list(proposal.graph_facts or [])
            existing_keys = {
                (fact.subject, fact.predicate, fact.object) for fact in merged_facts
            }
            for raw in (old_record.graph_facts or []):
                try:
                    from aiive.memory.graph_types import GraphFact

                    fact = GraphFact.model_validate(raw)
                except Exception:
                    logger.debug("合并旧记忆关系事实失败，跳过该条", exc_info=True)
                    continue
                if (fact.subject, fact.predicate, fact.object) in existing_keys:
                    continue
                merged_facts.append(fact)
            proposal.graph_facts = merged_facts
        # 保持原 proposal_id 与 idempotency_key：本次写入是同一个提案的最终落库，
        # 审计行（memory_proposals）已由建案阶段占用该幂等键，重算会产生第二行并
        # 触发 proposal_id 唯一约束冲突。
        return proposal

    # ------------------------------------------------------------------
    # 7. 案例状态辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _load_case_for_update(db: Session, case_id: str) -> MemoryConflictCase | None:
        """按主键锁定案例行：并发回复（Handler + API）只有一个能推进状态机。"""
        return (
            db.query(MemoryConflictCase)
            .filter(MemoryConflictCase.id == case_id)
            .with_for_update()
            .one_or_none()
        )

    @staticmethod
    def _load_old_record(db: Session, case: MemoryConflictCase) -> MemoryRecord | None:
        if not case.old_memory_id:
            return None
        return db.get(MemoryRecord, case.old_memory_id)

    @staticmethod
    def _is_expired(case: MemoryConflictCase, now: datetime | None = None) -> bool:
        expires_at = _as_utc(case.expires_at)
        return expires_at is not None and expires_at <= (now or _utcnow())

    @classmethod
    def _mark_status(
        cls,
        case: MemoryConflictCase,
        status: str,
        *,
        decision: ReplyDecision | None = None,
        reason: str = "",
        memory_id: str = "",
    ) -> None:
        case.status = status
        case.updated_at = _utcnow()
        if decision is not None:
            case.decision = decision.decision
            case.decision_span = (decision.reason or decision.decision)[:2000]
        case.resolution_reason = reason[:2000]
        if memory_id:
            case.resolved_memory_id = memory_id
        if status in (STATUS_RESOLVED, STATUS_REJECTED, STATUS_EXPIRED, STATUS_STALE):
            case.resolved_at = _utcnow()

    @staticmethod
    def _sync_proposal_record(
        db: Session,
        case: MemoryConflictCase,
        memory_id: str,
        final_op: str,
        content: str = "",
    ) -> None:
        """把提案审计行从 conflict_pending 更新为最终结果（同一幂等键，不新增行）。"""
        if not case.proposal_id:
            return
        row = (
            db.query(MemoryProposalModel)
            .filter(MemoryProposalModel.proposal_id == case.proposal_id)
            .one_or_none()
        )
        if row is None:
            return
        row.final_operation = final_op[:32]
        row.final_memory_id = memory_id or None
        if content:
            # 用户决策可能修订/合并内容：审计行同步为最终写入内容。
            row.content = content
        row.gate_reason = (row.gate_reason or "")[:500]

    @staticmethod
    def _close_notification(
        db: Session,
        case: MemoryConflictCase,
        *,
        status: str = "confirmed",
        resolution: str = "",
    ) -> None:
        """把该案例的通知置为终态，避免用户看到已处理的待办。

        status 取既有通知终态（`confirmed` / `cancelled`）：过期的案例用
        `cancelled`，因为它不再需要用户回答。
        """
        events = (
            db.query(Event)
            .filter(
                Event.trace_id == case.id,
                Event.event_type == "notification_created",
            )
            .all()
        )
        for event in events:
            payload = dict(event.payload or {})
            payload["status"] = status
            payload["resolution"] = resolution or case.status
            event.payload = payload
        if events:
            db.flush()

    # ------------------------------------------------------------------
    # 7.1 定期清扫：TTL 过期的待确认案例
    # ------------------------------------------------------------------

    @classmethod
    def expire_open_cases(
        cls,
        db: Session,
        *,
        limit: int = 200,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """把 TTL 已过的待确认案例批量置为 expired（幂等、可重复执行）。

        与 `publish_question` / `apply_decision` 的惰性过期语义一致，区别只是由
        后台定期清扫。过期案例不再需要用户回答，因此同时把它的通知置为
        `cancelled`（既有通知终态），避免前端留下永远点不完的待办。

        本方法只改案例与通知：不写记忆、不写关系，也不触发任何投影。
        """
        now = now or _utcnow()
        batch = max(1, int(limit))
        rows = (
            db.query(MemoryConflictCase)
            .filter(
                MemoryConflictCase.status.in_(list(_OPEN_STATUSES)),
                MemoryConflictCase.expires_at.isnot(None),
                MemoryConflictCase.expires_at <= now,
            )
            .order_by(MemoryConflictCase.expires_at.asc())
            .limit(batch)
            .with_for_update()
            .all()
        )
        expired = 0
        for case in rows:
            # 行锁内复核：并发回复可能已经把案例推进到终态。
            if case.status not in _OPEN_STATUSES or not cls._is_expired(case, now=now):
                continue
            cls._mark_status(case, STATUS_EXPIRED, reason="ttl_expired")
            cls._close_notification(
                db, case, status="cancelled", resolution=STATUS_EXPIRED,
            )
            expired += 1
        db.flush()
        return {"expired": expired, "scanned": len(rows), "has_more": len(rows) >= batch}

    # ------------------------------------------------------------------
    # 8. 查询（API / 诊断）
    # ------------------------------------------------------------------

    @staticmethod
    def list_cases(
        db: Session,
        *,
        thread_id: str = "",
        status: str = "",
        limit: int = 50,
    ) -> list[MemoryConflictCase]:
        query = db.query(MemoryConflictCase)
        if thread_id:
            query = query.filter(MemoryConflictCase.thread_id == thread_id)
        if status:
            query = query.filter(MemoryConflictCase.status == status)
        return (
            query.order_by(MemoryConflictCase.created_at.desc()).limit(max(1, limit)).all()
        )
