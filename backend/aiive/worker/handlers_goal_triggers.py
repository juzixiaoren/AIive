"""goal_trigger_decision Handler：一次触发 → 一次模型决策 → 服务端执行。

事务边界（与既有 Handler 一致）：
1. 短事务读取目标/步骤/上下文并校验；
2. 事务外调用模型；
3. 主动消息单独短事务写入（被前台 Turn 占用时返回 RETRY_LATER）；
4. 短事务执行计划变更并写 Trace。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from aiive.config import settings
from aiive.db.base import SessionLocal
from aiive.db.models import Event
from aiive.runtime.proactive_message import ProactiveBlocked
from aiive.task_runtime.goal_decision import (
    MESSAGE_ACTIONS,
    GoalDecisionEngine,
    decisions_today,
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


def _retry_later(reason: str) -> HandlerResult:
    return HandlerResult(
        HandlerOutcome.RETRY_LATER,
        reason,
        retry_available_at=datetime.now(timezone.utc) + timedelta(seconds=BUSY_RETRY_SECONDS),
    )


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

    trigger: dict[str, Any] = {
        "trigger_key": trigger_key,
        "reason": str(payload.get("reason") or ""),
        "fired_at": str(payload.get("fired_at") or ""),
        "event_type": None,
        "event_payload": None,
    }

    db = SessionLocal()
    try:
        task = TaskRepository(db).get_task(task_id)
        if task is None:
            return HandlerResult(
                HandlerOutcome.NON_RETRYABLE,
                "目标任务不存在",
                terminal_reason="goal_task_not_found",
            )
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
        if event_id:
            source_event = db.get(Event, event_id)
            if source_event is not None:
                trigger["event_type"] = source_event.event_type
                trigger["event_payload"] = dict(source_event.payload or {})

        engine = GoalDecisionEngine(db)
        context = engine.context(task, plan, step, trigger)
        messages = engine.messages(context)
        thread_id = task.thread_id
        db.commit()
    finally:
        db.close()

    try:
        decision, meta = engine.decide(messages, trace_id=trace_id)
    except Exception as error:
        logger.exception("目标触发决策调用模型失败: task_id=%s", task_id)
        return HandlerResult(
            HandlerOutcome.RETRYABLE_ERROR,
            f"goal_decision_llm_failed:{type(error).__name__}",
        )

    message_event_id = ""
    message_turn_id = ""
    db_apply = SessionLocal()
    try:
        apply_engine = GoalDecisionEngine(db_apply)
        # 主动消息与计划变更同一事务：被拒的决策不会先发消息，写消息失败则整体回滚。
        outcome = apply_engine.apply(
            task_id=task_id,
            step_id=step_id,
            trigger_key=trigger_key,
            decision=decision,
            message_text=decision.message if decision.action in MESSAGE_ACTIONS else "",
            trace_id=trace_id,
        )
        message_event_id = str(outcome.get("message_event_id") or "")
        message_turn_id = str(outcome.get("message_turn_id") or "")
        task = TaskRepository(db_apply).get_task(task_id)
        if task is not None:
            apply_engine.write_trace(
                task=task,
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
        # 前台 Turn 持有租约：本次事务的所有变更都未提交，finally 的 close() 即回滚，
        # 交给 Outbox 稍后重试，保持时间线顺序。
        return _retry_later("foreground_turn_active")
    except Exception:
        db_apply.rollback()
        logger.exception("目标决策执行失败: task_id=%s", task_id)
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, "goal_decision_apply_failed")
    finally:
        db_apply.close()

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
