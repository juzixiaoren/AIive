"""冲突闭环的 Outbox Handler：后台生成澄清问题 / 解析回复 / 清扫过期案例。

前两个 Handler 遵循"LLM 在事务外"的既有约定：

    Phase A（短事务）读快照 → Phase B（无会话）调用模型 → Phase C（短事务）落库

因此模型响应慢不会长期占着数据库事务；任何一步失败都按 Outbox 既有语义
重试或 deadletter，不会留下半成品状态（案例状态机是唯一真相源）。
"""
from __future__ import annotations

import logging

from aiive.context.run_context import RUN_CTX_OUTBOX_WORKER, RunContext
from aiive.core.llm_client import default_llm_client
from aiive.db.base import SessionLocal
from aiive.db.models import MemoryConflictCase
from aiive.memory.conflict_adjudication import (
    ConflictQuestionGenerator,
    ConflictReplyParser,
)
from aiive.memory.conflict_confirmation import MemoryConflictService
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome, HandlerResult

logger = logging.getLogger(__name__)

_OPEN_STATUSES = ("pending", "asking")


def _snapshot_question_input(case_id: str) -> tuple[bool, dict[str, str], str]:
    """Phase A：读取生成问题所需的最小快照（不持有会话）。"""
    db = SessionLocal()
    try:
        case = db.get(MemoryConflictCase, case_id)
        if case is None:
            return False, {}, "missing_case"
        if case.status not in _OPEN_STATUSES:
            return False, {}, f"case_not_open:{case.status}"
        return True, {
            "old_content": case.old_content or "",
            "new_content": case.new_content or "",
            "reason": case.verdict_reason or "",
            "question": case.question or "",
        }, ""
    finally:
        db.close()


def handle_memory_conflict_question(claimed: ClaimedJob) -> HandlerResult:
    """为待确认案例生成澄清问题并通过通知机制提问（幂等）。"""
    case_id = str(claimed.payload.get("conflict_case_id", ""))
    if not case_id:
        return HandlerResult(
            HandlerOutcome.NON_RETRYABLE,
            "澄清问题任务缺少 conflict_case_id",
            terminal_reason="invalid_payload",
        )
    ok, snapshot, reason = _snapshot_question_input(case_id)
    if not ok:
        if reason == "missing_case":
            return HandlerResult(
                HandlerOutcome.NON_RETRYABLE, reason, terminal_reason="conflict_case_missing",
            )
        # 案例已关闭（用户已回复 / 已过期）：任务自然完成，不重复通知。
        return HandlerResult(HandlerOutcome.COMPLETED, reason)

    # Phase B：事务外生成问题（失败时生成器内部退化为确定性兜底问题）。
    question = snapshot.get("question", "")
    if not question:
        try:
            question = ConflictQuestionGenerator(default_llm_client()).generate(
                old_content=snapshot.get("old_content", ""),
                new_content=snapshot.get("new_content", ""),
                reason=snapshot.get("reason", ""),
                trace_id=claimed.trace_id or case_id,
            )
        except Exception as exc:
            logger.exception("澄清问题生成异常: case=%s", case_id)
            return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, str(exc))

    # Phase C：落库通知 + 状态推进。
    db = SessionLocal()
    try:
        result = MemoryConflictService.publish_question(
            db, case_id, llm_client=None, question=question,
        )
        db.commit()
        return HandlerResult(HandlerOutcome.COMPLETED, str(result))
    except Exception as exc:
        db.rollback()
        logger.exception("澄清问题发布失败: case=%s", case_id)
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, str(exc))
    finally:
        db.close()


def _snapshot_reply_input(case_id: str) -> tuple[bool, dict[str, str], str]:
    """Phase A：读取解析回复所需的最小快照。"""
    db = SessionLocal()
    try:
        case = db.get(MemoryConflictCase, case_id)
        if case is None:
            return False, {}, "missing_case"
        if case.status not in _OPEN_STATUSES:
            return False, {}, f"case_not_open:{case.status}"
        return True, {
            "question": case.question or "",
            "old_content": case.old_content or "",
            "new_content": case.new_content or "",
        }, ""
    finally:
        db.close()


def handle_memory_conflict_reply(claimed: ClaimedJob) -> HandlerResult:
    """解析用户自然语言回复并执行决策（写入仍走 MemoryWriteService）。"""
    payload = claimed.payload
    case_id = str(payload.get("conflict_case_id", ""))
    thread_id = str(payload.get("thread_id", ""))
    user_message = str(payload.get("user_message", ""))
    if not case_id or not thread_id or not user_message.strip():
        return HandlerResult(
            HandlerOutcome.NON_RETRYABLE,
            "回复任务缺少 conflict_case_id / thread_id / user_message",
            terminal_reason="invalid_payload",
        )
    ok, snapshot, reason = _snapshot_reply_input(case_id)
    if not ok:
        if reason == "missing_case":
            return HandlerResult(
                HandlerOutcome.NON_RETRYABLE, reason, terminal_reason="conflict_case_missing",
            )
        return HandlerResult(HandlerOutcome.COMPLETED, reason)

    # Phase B：事务外解析（失败时按 unrelated 处理，不改变任何记忆）。
    try:
        decision = ConflictReplyParser(default_llm_client()).parse(
            question=snapshot.get("question", ""),
            old_content=snapshot.get("old_content", ""),
            new_content=snapshot.get("new_content", ""),
            user_reply=user_message,
            trace_id=claimed.trace_id or case_id,
        )
    except Exception as exc:
        logger.exception("冲突回复解析异常: case=%s", case_id)
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, str(exc))

    run_ctx = RunContext(
        thread_id=thread_id,
        trace_id=claimed.trace_id or case_id,
        source=RUN_CTX_OUTBOX_WORKER,
        turn_id=str(payload.get("turn_id", "")),
        turn_record_id=str(payload.get("turn_record_id", "")),
        execution_mode="user_required",
        source_event_ids=[str(item) for item in (payload.get("source_event_ids") or [])],
    )

    # Phase C：重新校验案例状态与记忆版本后执行正式写入。
    db = SessionLocal()
    try:
        outcome = MemoryConflictService.apply_decision(
            db, case_id=case_id, decision=decision, run_context=run_ctx,
            source="user_reply",
        )
        db.commit()
        logger.info(
            "冲突回复处理完成: case=%s decision=%s status=%s memory=%s",
            case_id, decision.decision, outcome.status, outcome.memory_id,
        )
        return HandlerResult(HandlerOutcome.COMPLETED, str(outcome.to_dict()))
    except Exception as exc:
        db.rollback()
        logger.exception("冲突回复执行失败: case=%s", case_id)
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, str(exc))
    finally:
        db.close()


def handle_memory_conflict_cleanup(claimed: ClaimedJob) -> HandlerResult:
    """清扫 TTL 已过的待确认案例（幂等；只改案例与通知，不碰记忆与图谱）。

    由调度器按时间窗口入队，`batch_size` 控制单次处理量；仍有剩余时返回
    CONTINUE 让 Worker 继续分页，避免一个事务里锁住大量案例行。
    """
    try:
        batch_size = int(claimed.payload.get("batch_size", 200) or 200)
    except (TypeError, ValueError):
        batch_size = 200
    db = SessionLocal()
    try:
        result = MemoryConflictService.expire_open_cases(db, limit=batch_size)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("冲突案例过期清扫失败")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, str(exc))
    finally:
        db.close()
    detail = f"expired={result['expired']} scanned={result['scanned']}"
    if result["has_more"]:
        return HandlerResult(HandlerOutcome.CONTINUE, detail)
    return HandlerResult(HandlerOutcome.COMPLETED, detail)
