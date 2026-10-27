"""步骤触发规则的纯逻辑：事件匹配、时间推进、幂等键与频率上限。

本模块不访问数据库、不调用 LLM：只把"规则 + 当前时间 + 相关事件 + 步骤状态"
换算成一次确定性触发结论，供 GoalTriggerService 落库并入队。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any

from aiive.task_runtime.goal_plan import (
    GoalStep,
    TriggerKind,
    TriggerRule,
    next_time_trigger,
    parse_datetime,
    timezone_for,
)

# 触发自身产生的内部事件不能反过来触发规则，否则会形成自激循环。
# 注意：goal_step_completed 是"已定义的评估时机"，是否参与由规则自身的
# evaluate_on_step_done 决定，不能无条件排除。
SELF_TRIGGER_EVENT_TYPES = frozenset({
    "goal_trigger_fired",
    "goal_trigger_decision",
    "goal_decision",
    "goal_step_running",
    "goal_step_due",
})

STEP_DONE_EVENT_TYPE = "goal_step_completed"


@dataclass(frozen=True)
class TriggerEvent:
    """参与条件匹配的最小事件视图（聊天事件或任务事件）。"""

    id: str
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class TriggerFiring:
    """一次确定性触发结论；key 是跨进程稳定的幂等标识。"""

    key: str
    reason: str
    fired_at: datetime
    next_trigger_at: datetime | None = None
    event_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TriggerExpiry:
    """窗口到期仍未满足条件：只记录过期，不触发 LLM。"""

    reason: str
    expired_at: datetime


def _set_next_trigger(step: GoalStep, value: str) -> bool:
    if step.next_trigger_at == value:
        return False
    step.next_trigger_at = value
    return True


def arm_rule(step: GoalStep, rule: TriggerRule, now: datetime) -> bool:
    """首次评估前补齐 armed_at 与时间型规则的 next_trigger_at；返回是否发生变更。"""
    changed = False
    if not step.armed_at:
        step.armed_at = now.astimezone(timezone.utc).isoformat()
        changed = True
    if rule.kind not in {TriggerKind.ONCE_AT, TriggerKind.RECURRING, TriggerKind.WINDOW_ONCE}:
        return changed
    if rule.kind == TriggerKind.WINDOW_ONCE:
        # 窗口只表达"从何时开始评估"，真正触发由条件命中决定。
        start = parse_datetime(rule.start)
        if start is not None:
            changed = _set_next_trigger(step, start.astimezone(timezone.utc).isoformat()) or changed
        return changed
    due = next_time_trigger(rule, now)
    if due is not None:
        changed = _set_next_trigger(step, due.astimezone(timezone.utc).isoformat()) or changed
    return changed


def _parse(value: Any) -> datetime | None:
    return parse_datetime(value)


def match_event(rule: TriggerRule, event: TriggerEvent) -> bool:
    """事件类型 + payload 子集匹配；空 match 表示只看类型。"""
    if event.event_type in SELF_TRIGGER_EVENT_TYPES:
        return False
    if event.event_type == STEP_DONE_EVENT_TYPE and not rule.evaluate_on_step_done:
        return False
    if rule.event_types and event.event_type not in rule.event_types:
        return False
    for key, expected in rule.match.items():
        if (event.payload or {}).get(key) != expected:
            return False
    return True


def _matching_events(rule: TriggerRule, events: list[TriggerEvent], *, after: datetime) -> list[TriggerEvent]:
    selected = [item for item in events if item.created_at >= after and match_event(rule, item)]
    selected.sort(key=lambda item: (item.created_at, item.id))
    return selected


def _within_caps(rule: TriggerRule, step: GoalStep, now: datetime) -> bool:
    """不定期跟进的冷却与频率上限：按步骤自己记录的历史时间戳计算。"""
    if rule.kind != TriggerKind.OPPORTUNISTIC:
        return True
    cooldown_until = _parse(step.cooldown_until)
    if cooldown_until is not None and cooldown_until > now:
        return False
    hits: list[datetime] = []
    for raw in step.opportunistic_hits:
        moment = _parse(raw)
        if moment is not None:
            hits.append(moment)
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    if sum(1 for item in hits if item > day_ago) >= rule.max_per_day:
        return False
    if sum(1 for item in hits if item > week_ago) >= rule.max_per_week:
        return False
    return True


def _latest_occurrence(rule: TriggerRule, now: datetime) -> datetime | None:
    """recurring：最近一次已经到点的日历时刻（不早于 until 之外）。"""
    zone = timezone_for(rule.timezone)
    moment = now.astimezone(zone)
    clock = dtime.fromisoformat(rule.time or "00:00")
    candidate = datetime.combine(moment.date(), clock, tzinfo=zone)
    if rule.frequency == "weekly":
        weekday = rule.weekday if rule.weekday is not None else 0
        candidate -= timedelta(days=(moment.weekday() - weekday) % 7)
        if candidate > moment:
            candidate -= timedelta(days=7)
    elif candidate > moment:
        candidate -= timedelta(days=1)
    until = parse_datetime(rule.until) if rule.until else None
    if until is not None and candidate.date() > until.date():
        return None
    return candidate


def evaluate_rule(
    step: GoalStep,
    rule: TriggerRule,
    now: datetime,
    events: list[TriggerEvent],
) -> TriggerFiring | None:
    """返回一次触发结论；None 表示本轮条件未满足或已经触发过。"""
    armed = _parse(step.armed_at) or now
    key: str | None = None
    reason = ""
    event_id: str | None = None
    metadata: dict[str, Any] = {}

    if rule.kind == TriggerKind.ONCE_AT:
        target = _parse(rule.at)
        # 计划确认时间晚于 at 时也补触发一次：一次性规则不因扫描延迟而永久丢失。
        if target is None or now < target:
            return None
        key = f"once_at:{target.astimezone(timezone.utc).isoformat()}"
        reason = "once_at"
    elif rule.kind == TriggerKind.RECURRING:
        occurrence = _latest_occurrence(rule, now)
        if occurrence is None or occurrence < armed:
            return None
        key = f"recurring:{occurrence.astimezone(timezone.utc).isoformat()}"
        reason = "recurring"
        nxt = next_time_trigger(rule, now)
        metadata["next_trigger_at"] = nxt.astimezone(timezone.utc).isoformat() if nxt else None
    elif rule.kind == TriggerKind.WINDOW_ONCE:
        if step.expired_at or step.trigger_count > 0:
            # 窗口内只触发一次：已触发过或已过期都不再重新判定。
            return None
        start = _parse(rule.start)
        end = _parse(rule.end)
        if start is None or end is None or now < start:
            return None
        if now > end:
            return None
        if rule.event_types:
            matched = _matching_events(rule, events, after=max(start, armed))
            if not matched:
                return None
            event_id = matched[0].id
            key = f"window_once:{start.astimezone(timezone.utc).isoformat()}:{event_id}"
            metadata["event_type"] = matched[0].event_type
        else:
            key = f"window_once:{start.astimezone(timezone.utc).isoformat()}:always"
        reason = "window_once"
    elif rule.kind == TriggerKind.EVENT_CONDITION:
        matched = _matching_events(rule, events, after=armed)
        if not matched:
            return None
        event_id = matched[0].id
        key = f"event_condition:{event_id}"
        reason = "event_condition"
        metadata["event_type"] = matched[0].event_type
    elif rule.kind == TriggerKind.OPPORTUNISTIC:
        if not _within_caps(rule, step, now):
            return None
        after = max(armed, _parse(step.cooldown_until) or armed)
        matched = _matching_events(rule, events, after=after)
        if not matched:
            return None
        event_id = matched[0].id
        key = f"opportunistic:{event_id}"
        reason = "opportunistic"
        metadata["event_type"] = matched[0].event_type

    if key is None:
        return None
    if step.last_trigger_key == key:
        return None
    return TriggerFiring(
        key=key,
        reason=reason,
        fired_at=now,
        next_trigger_at=(
            _parse(metadata.get("next_trigger_at")) if metadata.get("next_trigger_at") else None
        ),
        event_id=event_id,
        metadata=metadata,
    )


def expiry_of(step: GoalStep, rule: TriggerRule, now: datetime) -> TriggerExpiry | None:
    """窗口到期且从未触发：标记过期，不调用 LLM、不再排期。"""
    if rule.kind != TriggerKind.WINDOW_ONCE:
        return None
    if step.expired_at or step.trigger_count > 0:
        return None
    end = _parse(rule.end)
    if end is None or now <= end:
        return None
    return TriggerExpiry(reason="window_expired", expired_at=now)
