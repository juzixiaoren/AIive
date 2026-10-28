"""goal_trigger_decision Handler：一次触发 → 一次模型决策 → 服务端执行。

事务与并发边界（V57）：
1. 短事务读取目标/步骤/上下文，并读取已持久化的决策（有则不再调用模型）；
2. **先抢占线程级占用**（与前台 Turn 共用同一互斥行）才允许调用模型，
   占用持有到决策应用与主动消息落库完成——期间前台请求让路等待，
   不会出现“检查没有活跃 Turn → 调用 LLM”的并发窗口；
3. 模型返回后先持久化结构化决策（稳定标识 task_id+step_id+trigger_key），
   再在另一个短事务里应用计划变更 + 写主动消息 + 写“已应用”标记；
4. Outbox 重投：已应用 → 直接 COMPLETED（只收尾，不重复副作用）；
   已决策未应用 → 复用决策，不再调用模型。
5. 全程不跨模型调用持有数据库事务；占用带 owner/fencing/租约，崩溃后可接管。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from aiive.config import settings
from aiive.db.base import SessionLocal
from aiive.db.models import Event
from aiive.runtime.proactive_message import ProactiveBlocked
from aiive.runtime.thread_lease import (
    GOAL_DECISION_LEASE_SECONDS,
    OWNER_GOAL_DECISION,
    acquire_thread_lease,
    release_thread_lease,
)
from aiive.runtime.turn_events import (
    OPEN_STATUSES,
    STATUS_HANDLED,
    active_turn_for_thread,
    expire_pending_event,
    inbox_state,
)
from aiive.task_runtime.goal_decision import (
    MESSAGE_ACTIONS,
    GoalDecisionEngine,
    decision_id_for,
    decisions_today,
    load_persisted_decision,
    mark_decision_applied,
    persist_decision,
)
from aiive.task_runtime.goal_plan import FINISHED_STEP_STATES, GoalStatus
from aiive.task_runtime.goal_service import GoalService
from aiive.task_runtime.repository import TaskRepository
from aiive.task_runtime.schemas import TaskStatus
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome, HandlerResult

logger = logging.getLogger(__name__)

BUSY_TASK_STATES = frozenset({
    TaskStatus.DISPATCHING.value,
    TaskStatus.RUNNING.value,
    TaskStatus.VERIFYING.value,
    TaskStatus.RECONCILING.value,
})
BUSY_RETRY_SECONDS = 15
# 等待前台 Turn 时的重试上限：对齐租约到期时间，但不超过该上限，
# 避免 Turn 长时间运行时以 15s 级空转刷 Outbox（也避免无限重试的观感）。
FOREGROUND_WAIT_MAX_SECONDS = 60


def _retry_later(reason: str, until: datetime | None = None) -> HandlerResult:
    now = datetime.now(UTC)
    delay = timedelta(seconds=BUSY_RETRY_SECONDS)
    if until is not None:
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        # 稍晚于租约到期：Turn 正常收尾或崩溃失租后，下一次轮询即可接手。
        wait = until - now + timedelta(seconds=1)
        delay = min(timedelta(seconds=FOREGROUND_WAIT_MAX_SECONDS), max(delay, wait))
    return HandlerResult(
        HandlerOutcome.RETRY_LATER,
        reason,
        retry_available_at=now + delay,
    )


def _release_lease(thread_id: str, owner_id: str, fencing_token: int) -> None:
    """短事务交还占用；失败只记日志，租约到期后仍可被接管。"""
    db = SessionLocal()
    try:
        release_thread_lease(
            db, thread_id,
            owner_kind=OWNER_GOAL_DECISION,
            owner_id=owner_id,
            fencing_token=fencing_token,
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("释放线程级占用失败: thread_id=%s owner_id=%s", thread_id, owner_id)
    finally:
        db.close()


def _active_turn_until(thread_id: str) -> datetime | None:
    """升级前的运行中 Turn 没有占用行：仍需让路（返回其租约到期时间）。"""
    db = SessionLocal()
    try:
        turn = active_turn_for_thread(db, thread_id)
        return turn.lease_expires_at if turn is not None else None
    finally:
        db.close()


def handle_goal_trigger_decision(claimed: ClaimedJob) -> HandlerResult:
    payload = claimed.payload or {}
    task_id = str(payload.get("task_id") or "")
    step_id = str(payload.get("step_id") or "")
    trigger_key = str(payload.get("trigger_key") or "")
    trace_id = str(payload.get("trace_id") or claimed.trace_id or "")
    if not task_id or not step_id or not trigger_key:
        return HandlerResult(
            HandlerOutcome.NON_RETRYABLE,
            "goal_trigger_decision 缺少 task_id/step_id/trigger_key",
            terminal_reason="invalid_goal_trigger_payload",
        )

    raw_event_payload = payload.get("trigger_event_payload")
    trigger: dict[str, Any] = {
        "trigger_key": trigger_key,
        "reason": str(payload.get("reason") or ""),
        "fired_at": str(payload.get("fired_at") or ""),
        # 外部运行事件没有聊天 Event 行：类型与元数据直接由触发作业携带。
        "event_type": str(payload.get("trigger_event_type") or "") or None,
        "event_payload": dict(raw_event_payload) if isinstance(raw_event_payload, dict) else None,
    }

    # ── 阶段 1：短事务读取与守卫（不调用模型） ──
    db = SessionLocal()
    try:
        task = TaskRepository(db).get_task(task_id)
        if task is None:
            return HandlerResult(
                HandlerOutcome.NON_RETRYABLE,
                "目标任务不存在",
                terminal_reason="goal_task_not_found",
            )

        # 已应用过的决策：重投只做收尾，绝不重复改计划或重复发消息。
        persisted = load_persisted_decision(db, task_id, step_id, trigger_key)
        if persisted is not None and persisted.applied:
            return HandlerResult(HandlerOutcome.COMPLETED, "decision_already_applied")

        pending_key = str(payload.get("pending_event_dedupe_key") or "")
        if pending_key:
            state = inbox_state(db, pending_key)
            if state == STATUS_HANDLED:
                # 前台 Turn 已在安全边界内处理该事件（可能已主动提醒用户）：
                # 不再发起第二次模型决策，也不重复写主动消息。
                return HandlerResult(HandlerOutcome.COMPLETED, "handled_in_turn")
            # 除 handled 外的任何状态都必须先看前台 Turn：pending/injected/deferred
            # 是尚未处理，expired 是收件箱溢出淘汰或上一轮让出，空状态是入队时被拒
            # 或行已被淘汰。只要 Turn 仍持有有效租约，这里就绝不能调用模型——
            # 否则溢出淘汰恰恰会让 Outbox 绕过前台检查，形成并发决策。
            turn = active_turn_for_thread(db, task.thread_id)
            if turn is not None:
                return _retry_later("foreground_turn_active", until=turn.lease_expires_at)
            if state in OPEN_STATUSES:
                # Turn 已结束但事件未处理（崩溃/中断/延后）：标记 expired 后交回
                # 原决策路径，本次作业继续走一次 GoalDecisionEngine 兜底。
                expire_pending_event(db, pending_key)
                db.commit()
        goals = GoalService(db)
        plan = goals.load_plan(task)
        if plan is None or plan.status != GoalStatus.ACTIVE:
            return HandlerResult(HandlerOutcome.COMPLETED, "目标不在可推进状态")
        step = plan.step(step_id)
        if step is None:
            return HandlerResult(HandlerOutcome.COMPLETED, "步骤已不存在")
        if step.status in FINISHED_STEP_STATES:
            return HandlerResult(HandlerOutcome.COMPLETED, "步骤已完成")
        if step.last_trigger_key != trigger_key:
            return HandlerResult(HandlerOutcome.COMPLETED, "触发已被更新的规则覆盖")
        if task.status in BUSY_TASK_STATES:
            # 前台 Run 正在执行：让出本轮，稍后重新决策；不并发修改计划。
            return _retry_later(f"task_busy:{task.status}")
        if decisions_today(db, task.id) >= settings.aiive_goal_trigger_max_decisions_per_day:
            return HandlerResult(HandlerOutcome.COMPLETED, "当日决策次数已达上限")

        event_id = str(payload.get("trigger_event_id") or "")
        if event_id and trigger["event_payload"] is None:
            source_event = db.get(Event, event_id)
            if source_event is not None:
                trigger["event_type"] = source_event.event_type
                trigger["event_payload"] = dict(source_event.payload or {})

        engine = GoalDecisionEngine(db)
        context = engine.context(task, plan, step, trigger)
        messages = engine.messages(context)
        thread_id = task.thread_id
        decision_id = (
            persisted.decision_id
            if persisted is not None
            else decision_id_for(task_id, step_id, trigger_key)
        )
        db.commit()
    finally:
        db.close()

    # ── 阶段 2：抢占线程级占用（与前台 Turn 共用互斥行） ──
    lease = acquire_thread_lease(
        thread_id,
        owner_kind=OWNER_GOAL_DECISION,
        owner_id=decision_id,
        ttl_seconds=GOAL_DECISION_LEASE_SECONDS,
    )
    if lease is None:
        return _retry_later("thread_lease_busy")

    try:
        # 升级前启动的 Turn 可能没有占用行：拿到占用后再确认一次，
        # 仍在有效租约内就让路，绝不在同一线程并发调用模型。
        active_until = _active_turn_until(thread_id)
        if active_until is not None:
            return _retry_later("foreground_turn_active", until=active_until)

        # ── 阶段 3：模型决策（已持久化则复用，不再调用模型） ──
        if persisted is None:
            try:
                decision, meta = engine.decide(messages, trace_id=trace_id)
            except Exception as error:
                logger.exception("目标触发决策调用模型失败: task_id=%s", task_id)
                return HandlerResult(
                    HandlerOutcome.RETRYABLE_ERROR,
                    f"goal_decision_llm_failed:{type(error).__name__}",
                )
            db_decision = SessionLocal()
            try:
                persist_decision(
                    db_decision,
                    task_id=task_id,
                    step_id=step_id,
                    trigger_key=trigger_key,
                    decision=decision,
                    meta=meta,
                )
                db_decision.commit()
            except Exception:
                db_decision.rollback()
                logger.exception("目标决策持久化失败: task_id=%s", task_id)
                return HandlerResult(
                    HandlerOutcome.RETRYABLE_ERROR, "goal_decision_persist_failed",
                )
            finally:
                db_decision.close()
        else:
            decision = persisted.decision
            meta = persisted.meta

        # ── 阶段 4：应用决策（计划变更 + 主动消息 + 已应用标记同一事务） ──
        message_event_id = ""
        message_turn_id = ""
        outcome: dict[str, Any] = {}
        db_apply = SessionLocal()
        try:
            apply_engine = GoalDecisionEngine(db_apply)
            outcome = apply_engine.apply(
                task_id=task_id,
                step_id=step_id,
                trigger_key=trigger_key,
                decision=decision,
                message_text=decision.message if decision.action in MESSAGE_ACTIONS else "",
                trace_id=trace_id,
                decision_id=decision_id,
            )
            message_event_id = str(outcome.get("message_event_id") or "")
            message_turn_id = str(outcome.get("message_turn_id") or "")
            mark_decision_applied(
                db_apply, task_id=task_id, decision_id=decision_id, outcome=outcome,
            )
            task_row = TaskRepository(db_apply).get_task(task_id)
            if task_row is not None:
                apply_engine.write_trace(
                    task=task_row,
                    step=step,
                    trigger=trigger,
                    messages=messages,
                    decision=decision,
                    meta=meta,
                    trace_id=trace_id,
                    outcome=outcome,
                )
            db_apply.commit()
        except ProactiveBlocked:
            # 前台 Turn 持有租约：本次事务的所有变更都未提交，交给 Outbox 稍后
            # 重试（决策已持久化，重试不会再调用模型）。
            db_apply.rollback()
            return _retry_later("foreground_turn_active")
        except Exception:
            db_apply.rollback()
            logger.exception("目标决策执行失败: task_id=%s", task_id)
            return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, "goal_decision_apply_failed")
        finally:
            db_apply.close()
    finally:
        _release_lease(thread_id, decision_id, lease.fencing_token)

    if message_event_id:
        try:
            from aiive.api.ws_manager import ws_manager

            ws_manager.broadcast_to_thread_sync(
                thread_id,
                "new_message",
                {
                    "event_id": message_event_id,
                    "reply": decision.message,
                    "thread_id": thread_id,
                    "trace_id": trace_id,
                    "turn_id": message_turn_id,
                    "action_cards": [],
                    "tool_calls": [],
                    "proactive": True,
                },
            )
        except Exception:
            logger.exception("主动消息推送失败（已落库）: task_id=%s", task_id)

    return HandlerResult(
        HandlerOutcome.COMPLETED,
        f"goal_decision_{outcome.get('status')}",
    )
