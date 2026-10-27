"""长期目标触发扫描：把"规则满足"确定性转成 Outbox 决策任务。

职责边界：
- 只做确定性判断（时间/事件/冷却/上限），不调用 LLM、不执行步骤；
- 触发即写稳定幂等键（step.last_trigger_key）与 AgentTaskEvent，再入队
  goal_trigger_decision；重复扫描、进程重启都不会重复调用模型；
- 任务正在执行时跳过本轮，下一轮重新评估，事件不会被丢掉。
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import AgentTask, AgentTaskEvent, Event, OutboxJob
from aiive.task_runtime.goal_events import ACTIVE_GOAL_STATES, GoalEvent
from aiive.task_runtime.goal_plan import (
    FINISHED_STEP_STATES,
    dependencies_met,
    GOAL_TASK_TYPE,
    GoalPlan,
    GoalStatus,
    GoalStep,
    GoalStepStatus,
    TriggerKind,
    parse_datetime,
    trigger_of,
)
from aiive.task_runtime.goal_service import GoalService
from aiive.task_runtime.goal_triggers import (
    SELF_TRIGGER_EVENT_TYPES,
    STEP_DONE_EVENT_TYPE,
    TriggerEvent,
    TriggerFiring,
    arm_rule,
    evaluate_rule,
    expiry_of,
    match_event,
)
from aiive.task_runtime.repository import TaskRepository, utcnow

logger = logging.getLogger(__name__)

JOB_TYPE = "goal_trigger_decision"
TRIGGER_EVENT_LIMIT = 300
# 只扫描"空闲/等用户"的目标：运行中的任务让出本轮，下一轮重新评估（事件不会丢）。
ACTIVE_TASK_STATES = ACTIVE_GOAL_STATES
# 只有这三类规则由"事件到达"驱动；时间型规则仍由周期扫描负责。
EVENT_RULE_KINDS = frozenset({
    TriggerKind.EVENT_CONDITION,
    TriggerKind.WINDOW_ONCE,
    TriggerKind.OPPORTUNISTIC,
})


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _trigger_operation_id(task_id: str, step_id: str, key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"goal_trigger:{task_id}:{step_id}:{digest}"


def _trigger_trace_id(operation_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, operation_id))


class GoalTriggerService:
    """目标步骤触发规则的唯一确定性扫描入口。"""

    def __init__(self, db: Session):
        self.db: Session = db
        self.repo: TaskRepository = TaskRepository(db)
        self.goals: GoalService = GoalService(db)

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------
    def scan(self, *, now: datetime | None = None, limit: int = 50) -> int:
        """扫描活跃目标并触发到期规则；返回本轮新触发的数量。"""
        moment = now or utcnow()
        tasks = self.db.query(AgentTask).filter(
            AgentTask.task_type == GOAL_TASK_TYPE,
            AgentTask.status.in_(tuple(ACTIVE_TASK_STATES)),
        ).order_by(AgentTask.updated_at.asc()).limit(limit).with_for_update(skip_locked=True).all()
        fired = 0
        for task in tasks:
            try:
                fired += self._scan_task(task, moment)
            except Exception:
                # 单个目标失败不影响其他目标；下一轮会重新评估同一触发键。
                self.db.rollback()
                logger.exception("目标触发扫描失败: task_id=%s", task.id)
        self.db.flush()
        return fired

    def _scan_task(self, task: AgentTask, now: datetime) -> int:
        plan = self.goals.load_plan(task)
        if plan is None or plan.status != GoalStatus.ACTIVE:
            return 0
        events = self._thread_events(task, plan)
        done_event = self._step_done_event(task, plan)
        if done_event is not None:
            events = [*events, done_event]
        changed = False
        fired = 0
        for step in plan.steps:
            rule = trigger_of(step)
            if rule is None:
                continue
            if step.status in FINISHED_STEP_STATES or step.status == GoalStepStatus.RUNNING.value:
                continue
            # 依赖未满足时不能触发：否则会绕过计划顺序（用户沉默只阻塞依赖它的步骤）。
            if not dependencies_met(plan, step):
                continue
            changed = arm_rule(step, rule, now) or changed
            expired = expiry_of(step, rule, now)
            if expired is not None:
                # 过期也要走一次决策：让 LLM 判断是否需要告知用户、调整计划或结束步骤。
                # 幂等键由窗口起点决定，重复扫描不会二次唤醒模型。
                self._mark_expired(task, step, now)
                firing = TriggerFiring(
                    key=expired.key,
                    reason=expired.reason,
                    fired_at=now,
                    metadata={"window_end": rule.end},
                )
                if self._fire(task, step, rule, firing, now):
                    fired += 1
                changed = True
                continue
            firing = evaluate_rule(step, rule, now, events)
            if firing is None:
                continue
            # _fire 一定会改写 last_trigger_key，因此无论是否新建 job 都要落盘计划。
            if self._fire(task, step, rule, firing, now):
                fired += 1
            changed = True
        if changed:
            self.goals.save_plan(task, plan)
        return fired

    # ------------------------------------------------------------------
    # 外部事件投递（Outbox goal_event_dispatch 的消费入口）
    # ------------------------------------------------------------------
    def dispatch_event(self, event: GoalEvent, *, limit: int = 200) -> int:
        """把一条已投递的运行事件确定性匹配到活动计划；返回新触发数。

        只有事件类型 + 目标关联（线程/全局）+ 规则条件全部命中才会入队决策；
        没有订阅者时不调用 LLM。逐目标提交：单个目标失败不影响其他目标，
        已产生的决策作业也不会因回滚丢失（Outbox 重试时按幂等键去重）。
        """
        moment = utcnow()
        query = self.db.query(AgentTask).filter(
            AgentTask.task_type == GOAL_TASK_TYPE,
            AgentTask.status.in_(tuple(ACTIVE_TASK_STATES)),
        )
        if event.thread_id:
            # 线程级事件只唤醒同线程目标；全局事件（知识维护/自进化）对所有目标可见。
            query = query.filter(AgentTask.thread_id == event.thread_id)
        tasks = query.order_by(AgentTask.updated_at.asc()).limit(limit).with_for_update(
            skip_locked=True,
        ).all()
        fired = 0
        for task in tasks:
            try:
                fired += self._dispatch_task(task, event, moment)
            except Exception:
                self.db.rollback()
                logger.exception("目标事件匹配失败: task_id=%s event=%s", task.id, event.event_type)
                continue
            self.db.commit()
        return fired

    def _dispatch_task(self, task: AgentTask, event: GoalEvent, now: datetime) -> int:
        plan = self.goals.load_plan(task)
        if plan is None or plan.status != GoalStatus.ACTIVE:
            return 0
        view = TriggerEvent(
            id=event.event_id,
            event_type=event.event_type,
            payload=dict(event.payload or {}),
            created_at=event.occurred_at,
        )
        changed = False
        fired = 0
        for step in plan.steps:
            rule = trigger_of(step)
            if rule is None or rule.kind not in EVENT_RULE_KINDS:
                continue
            if step.status in FINISHED_STEP_STATES or step.status == GoalStepStatus.RUNNING.value:
                continue
            if not dependencies_met(plan, step):
                continue
            changed = arm_rule(step, rule, now) or changed
            if not match_event(rule, view):
                continue
            firing = evaluate_rule(step, rule, now, [view])
            if firing is None:
                continue
            # 事件元数据随决策作业传递，避免消费者再回查不存在的事件行。
            firing = TriggerFiring(
                key=firing.key,
                reason=firing.reason,
                fired_at=firing.fired_at,
                next_trigger_at=firing.next_trigger_at,
                event_id=firing.event_id,
                metadata={**firing.metadata, "event_payload": view.payload},
            )
            if self._fire(task, step, rule, firing, now):
                fired += 1
            changed = True
        if changed:
            self.goals.save_plan(task, plan)
        return fired

    # ------------------------------------------------------------------
    # 触发落库
    # ------------------------------------------------------------------
    def _fire(
        self,
        task: AgentTask,
        step: GoalStep,
        rule: Any,
        firing: Any,
        now: datetime,
    ) -> bool:
        operation_id = _trigger_operation_id(task.id, step.id, firing.key)
        trace_id = _trigger_trace_id(operation_id)
        step.last_trigger_key = firing.key
        step.last_trigger_at = _iso(now)
        if firing.next_trigger_at is not None:
            step.next_trigger_at = _iso(firing.next_trigger_at)
        elif rule.kind.value in {"once_at", "window_once", "event_condition", "opportunistic"}:
            # 一次性/窗口/事件型触发不再按时间排期；到期决策同样清空排期。
            step.next_trigger_at = None
        if rule.kind.value == "opportunistic":
            step.cooldown_until = _iso(now + timedelta(hours=rule.cooldown_hours))
            step.opportunistic_hits = [
                *step.opportunistic_hits[-(rule.max_per_week * 2):],
                _iso(now),
            ]
        self.repo.append_event(
            task.id,
            "goal_trigger_fired",
            {
                "step_id": step.id,
                "trigger_key": firing.key,
                "reason": firing.reason,
                "event_id": firing.event_id,
                "next_trigger_at": step.next_trigger_at,
                "trigger_count": step.trigger_count,
            },
            visibility="internal",
            dedupe_key=f"trigger:{operation_id}",
        )
        existing = self.db.query(OutboxJob).filter(
            OutboxJob.operation_id == operation_id,
        ).one_or_none()
        created = existing is None
        if created:
            # 只在真正新入队时累加次数，重复扫描/回放不虚增统计。
            step.trigger_count += 1
            self.db.add(OutboxJob(
                operation_id=operation_id,
                job_type=JOB_TYPE,
                status="pending",
                payload={
                    "task_id": task.id,
                    "step_id": step.id,
                    "trigger_key": firing.key,
                    "reason": firing.reason,
                    "trigger_event_id": firing.event_id,
                    # 外部运行事件没有聊天 Event 行：类型与元数据随载荷传递，
                    # 详细上下文仍由决策消费者按需读取（计划/Evidence/对话）。
                    "trigger_event_type": firing.metadata.get("event_type"),
                    "trigger_event_payload": firing.metadata.get("event_payload"),
                    "fired_at": _iso(now),
                    "trace_id": trace_id,
                },
                trace_id=trace_id,
                schema_version=1,
                max_retries=3,
            ))
        self.db.flush()
        return created

    def _mark_expired(self, task: AgentTask, step: GoalStep, now: datetime) -> None:
        step.expired_at = _iso(now)
        if step.status not in FINISHED_STEP_STATES:
            step.status = GoalStepStatus.MISSED
            step.missed_count += 1
        self.repo.append_event(
            task.id,
            "goal_trigger_expired",
            {"step_id": step.id, "expired_at": step.expired_at},
            visibility="conversation",
            dedupe_key=f"trigger_expired:{step.id}",
        )

    # ------------------------------------------------------------------
    # 事件视图
    # ------------------------------------------------------------------
    def _thread_events(self, task: AgentTask, plan: GoalPlan) -> list[TriggerEvent]:
        """读取线程内与规则相关的事件；只保留最老 armed_at 之后的部分。"""
        armed_candidates = []
        for step in plan.steps:
            if step.armed_at:
                moment = parse_datetime(step.armed_at)
                if moment is not None:
                    armed_candidates.append(moment)
        since = min(armed_candidates) if armed_candidates else None
        query = self.db.query(Event).filter(
            Event.thread_id == task.thread_id,
            Event.event_type.notin_(tuple(SELF_TRIGGER_EVENT_TYPES)),
        )
        if since is not None:
            query = query.filter(Event.created_at >= since)
        rows = query.order_by(Event.created_at.desc()).limit(TRIGGER_EVENT_LIMIT).all()
        events: list[TriggerEvent] = []
        for row in reversed(rows):
            created = row.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            events.append(TriggerEvent(
                id=row.id,
                event_type=row.event_type,
                payload=dict(row.payload or {}),
                created_at=created,
            ))
        return events

    def _step_done_event(self, task: AgentTask, plan: GoalPlan) -> TriggerEvent | None:
        """把"某一步刚完成"当作一次评估时机（仍由规则自身的冷却/上限把关）。"""
        armed = [
            parse_datetime(step.armed_at) for step in plan.steps if step.armed_at
        ]
        since = min((item for item in armed if item is not None), default=None)
        query = self.db.query(AgentTaskEvent).filter(
            AgentTaskEvent.task_id == task.id,
            AgentTaskEvent.event_type == STEP_DONE_EVENT_TYPE,
        )
        if since is not None:
            query = query.filter(AgentTaskEvent.created_at >= since)
        row = query.order_by(AgentTaskEvent.created_at.desc()).first()
        if row is None:
            return None
        created = row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        payload = dict(row.payload or {})
        return TriggerEvent(
            id=f"step-done:{row.id}",
            event_type="goal_step_completed",
            payload=payload,
            created_at=created,
        )
