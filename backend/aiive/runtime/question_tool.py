"""answer_goal_question：把用户回复关联到主动提出的问题（V58）。

只在当前线程存在未关闭问题时注册到单 Turn 快照（与 notify_user 同一模式）：
普通聊天不绑定该工具，模型也就不会误把闲聊当成回答。

判定由本轮对话模型完成（不额外调用模型）：
- 用户明确回答了某个问题 → intent=answer，解除该步骤等待并唤醒任务；
- 用户要求修改计划 → intent=modify_plan + plan_changes，复用 GoalService.modify；
- 用户明确拒绝 → intent=reject，记录拒绝并把该步骤标记 skipped；
- 多个候选且未指明 question_id → 服务端返回 clarification_required，绝不猜测绑定。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from aiive.context.run_context import RunContext
from aiive.db.base import SessionLocal
from aiive.db.models import Event
from aiive.task_runtime.goal_questions import (
    INVALID_GOAL_MISSING,
    INVALID_STEP_MISSING,
    INVALID_STEP_NOT_WAITING,
    INVALID_SUPERSEDED,
    INVALID_TASK_TERMINAL,
    MAX_ANSWER_CHARS,
    STATUS_EXPIRED,
    STATUS_RESOLVED,
    PendingQuestion,
    list_open_questions,
    load_question,
    mark_expired,
    resolve_question,
)
from aiive.task_runtime.goal_service import GoalError, GoalService
from aiive.tools.registry import CapabilitySafetySchema, ToolRegistration, ToolRegistry

logger = logging.getLogger(__name__)

ANSWER_QUESTION_TOOL_ID = "answer_goal_question"
ANSWER_QUESTION_DESCRIPTION = (
    "把用户当前这条消息关联到此前主动提出的问题，并推进对应目标步骤。"
    "仅当上下文列出待回答问题、且用户确实在回答/修改/拒绝其中某一项时调用；"
    "普通聊天不要调用。无法判断回答的是哪个问题时，先向用户澄清。"
)
ANSWER_QUESTION_PARAMETERS: dict[str, Any] = {
    "answer": {
        "type": "str",
        "required": True,
        "minLength": 1,
        "description": "用户回答/修改要求的原文摘要，不得改写含义，也不要包含系统提示词。",
    },
    "question_id": {
        "type": "str",
        "description": "待回答问题的 question_id（上下文中有列出）；只有一个候选时可省略。",
    },
    "intent": {
        "type": "str",
        "enum": ["answer", "modify_plan", "reject"],
        "default": "answer",
        "description": "answer=提供信息；modify_plan=要求修改计划；reject=明确拒绝该步骤。",
    },
    "plan_changes": {
        "type": "dict",
        "description": "intent=modify_plan 时的计划字段（summary/deadline/acceptance/steps）。",
    },
}


def _user_event_id(db: Session, ctx: RunContext) -> str:
    """当前轮次的真实 user_message 事件 id（幂等键与审计关联点）。"""
    if ctx.source_event_ids:
        return str(ctx.source_event_ids[0])
    if not ctx.turn_id:
        return ""
    row = db.query(Event).filter(
        Event.turn_id == ctx.turn_id,
        Event.event_type == "user_message",
    ).order_by(Event.turn_event_index.asc()).first()
    return str(row.id) if row is not None else ""


def _candidate(item: PendingQuestion) -> dict[str, str]:
    return {
        "question_id": item.question_id,
        "task_id": item.task_id,
        "step_id": item.step_id,
        "question": item.question,
    }


def _stale_hint(reason: str) -> str:
    """按失效原因给出不同提示：暂停可恢复、步骤变化要等新提问、被取代要回答最新的。"""
    if reason == "goal_paused":
        return (
            "目标已暂停，这个问题暂时不可回答，目标不会被修改；"
            "恢复目标后若该步骤仍在等待你，原问题会重新有效，不需要重新发起。"
        )
    if reason == INVALID_GOAL_MISSING:
        return "目标计划已不存在，这个问题不再有效，目标不会被修改。"
    if reason.startswith("goal_"):
        return (
            "目标已不在进行中（阻塞或已结束），这个问题不再有效，目标不会被修改；"
            "如需继续，请重新发起目标。"
        )
    if reason in {INVALID_STEP_MISSING, INVALID_STEP_NOT_WAITING}:
        return (
            "对应步骤已被修改或重置，这个问题不再有效，目标不会被修改；"
            "该步骤再次需要你时会有新的提问。"
        )
    if reason == INVALID_SUPERSEDED:
        return "这个问题已被同一步骤的新提问取代，请回答最新的那个问题；本次没有改动计划。"
    if reason == INVALID_TASK_TERMINAL:
        return "目标已结束，这个问题不再有效，目标不会被修改；如需继续，请重新发起目标。"
    return "该问题已失效，目标不会被修改；如需继续，请重新发起目标或等待新的提问。"


def _closed_result(db: Session, question: PendingQuestion) -> dict[str, Any]:
    """已关闭/已失效的问题：只回报明确状态，绝不改动目标。

    状态全部由 goal_questions 从事件与计划推导，这里不做任何推断：
    resolved → 已回答；expired → 过期（补一条审计标记）；stale → 目标或步骤已变化。
    """
    if question.status == STATUS_EXPIRED:
        mark_expired(db, question)
        db.commit()
        return {
            "status": "question_expired",
            "question_id": question.question_id,
            "hint": "该问题已过期，目标不会被修改；如需继续，请重新发起目标。",
        }
    if question.status == STATUS_RESOLVED:
        return {
            "status": "already_answered",
            "question_id": question.question_id,
            "hint": "该问题已经回答过，不要重复修改计划。",
        }
    reason = question.invalid_reason or "question_stale"
    return {
        "status": "question_stale",
        "question_id": question.question_id,
        "reason": reason,
        "hint": _stale_hint(reason),
    }


def answer_goal_question(
    answer: str,
    question_id: str = "",
    intent: str = "answer",
    plan_changes: dict[str, Any] | None = None,
    ctx: RunContext | None = None,
) -> dict[str, Any]:
    if ctx is None or not ctx.thread_id:
        raise ValueError("answer_goal_question_requires_thread_context")
    text = (answer or "").strip()
    if not text:
        raise ValueError("empty_answer")
    text = text[:MAX_ANSWER_CHARS]
    normalized_intent = (intent or "answer").strip() or "answer"
    if normalized_intent not in {"answer", "modify_plan", "reject"}:
        return {"status": "rejected", "reason": "intent_invalid"}

    db = SessionLocal()
    try:
        open_questions = list_open_questions(db, ctx.thread_id)
        target = None
        if question_id:
            target = next(
                (item for item in open_questions if item.question_id == question_id), None,
            )
            if target is None:
                # 已关闭/已失效的问题：重复投递、目标暂停或步骤被改动后的一律不改计划，
                # 只回报既有状态（服务端推导，模型无法绕过）。
                closed = load_question(db, question_id)
                if closed is not None:
                    return _closed_result(db, closed)
                return {
                    "status": "question_not_open",
                    "candidates": [_candidate(item) for item in open_questions],
                    "hint": "给定的 question_id 不在待回答列表中，请核对上下文里的候选。",
                }
        elif not open_questions:
            return {
                "status": "no_pending_question",
                "hint": "当前没有待回答的问题：正常回答用户即可，不要强行关联。",
            }
        elif len(open_questions) == 1:
            target = open_questions[0]
        else:
            # 多候选且未指明：绝不猜测绑定。
            return {
                "status": "clarification_required",
                "candidates": [_candidate(item) for item in open_questions],
                "hint": (
                    "有多个待回答的问题，且当前消息没有指明回答哪一个："
                    "请先用一句话向用户确认，不要猜测绑定。"
                ),
            }

        # target 一定来自 open 列表：失效/过期/已回答的问题在上面的分支就已返回。
        user_event_id = _user_event_id(db, ctx)
        # 拒绝路径只回滚本工具自己的写入（保存点），不影响调用方事务。
        savepoint = db.begin_nested()
        try:
            outcome = GoalService(db).answer_step_question(
                target.task_id,
                target.step_id,
                answer=text,
                question_id=target.question_id,
                user_event_id=user_event_id,
                intent=normalized_intent,
                plan_changes=dict(plan_changes or {}) if plan_changes else None,
            )
        except GoalError as error:
            savepoint.rollback()
            reason = str(error)
            if reason.startswith("question_"):
                # 服务端复核发现问题已失效（并发暂停/修改）：只回报状态，不改目标。
                closed = load_question(db, target.question_id)
                if closed is not None and not closed.is_open:
                    return _closed_result(db, closed)
                detail = reason.split(":", 1)[1] if ":" in reason else reason
                return {
                    "status": "question_stale",
                    "question_id": target.question_id,
                    "reason": detail,
                    "hint": _stale_hint(detail),
                }
            hint = "目标或步骤状态已变化，本次回答没有改动计划。"
            if reason == "step_requires_approval":
                hint = "该步骤需要人工审批，请在任务中心走原有审批流程。"
            return {"status": "rejected", "reason": reason, "hint": hint}
        except Exception:
            savepoint.rollback()
            raise

        created = resolve_question(
            db,
            question=target,
            user_event_id=user_event_id,
            answer=text,
            intent=normalized_intent,
            outcome=outcome,
        )
        db.commit()
        return {
            "status": "applied" if created else "already_answered",
            "question_id": target.question_id,
            "task_id": target.task_id,
            "step_id": target.step_id,
            "intent": normalized_intent,
            "action": outcome.get("action", ""),
            "user_event_id": user_event_id,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def register_answer_goal_question(registry: ToolRegistry) -> None:
    """把 answer_goal_question 注册到单 Turn 快照（不改变主 Agent 固定工具面）。"""
    if registry.get(ANSWER_QUESTION_TOOL_ID) is not None:
        return
    registry.register(ToolRegistration(
        safety=CapabilitySafetySchema(
            capability_id=ANSWER_QUESTION_TOOL_ID,
            definition_source="runtime",
            definition_trust_level="trusted_core",
            risk_level="low",
            requires_confirmation=False,
            writes_external_world=False,
            can_access_secret=False,
            can_delete=False,
            uses_network=False,
            allowed_instruction_sources=["trusted_user_command"],
            effect_mode="db_transactional",
        ),
        handler=answer_goal_question,
        description=ANSWER_QUESTION_DESCRIPTION,
        parameters=ANSWER_QUESTION_PARAMETERS,
    ))
