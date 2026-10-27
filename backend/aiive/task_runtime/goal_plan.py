"""长期目标计划：步骤、依赖、触发时间与进度的纯数据模型（无数据库依赖）。"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone
from enum import StrEnum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


GOAL_TASK_TYPE = "long_term_goal"
MAX_STEPS = 32
DEFAULT_TIMEZONE = "Asia/Shanghai"


class GoalStepKind(StrEnum):
    AUTONOMOUS = "autonomous"
    INTERACTIVE = "interactive"
    APPROVAL_REQUIRED = "approval_required"


class GoalStepStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    WAITING_TIME = "waiting_time"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    MISSED = "missed"


class GoalStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    REJECTED = "rejected"
    ABANDONED = "abandoned"


TERMINAL_GOAL_STATES = frozenset({
    GoalStatus.COMPLETED.value, GoalStatus.REJECTED.value, GoalStatus.ABANDONED.value,
})
FINISHED_STEP_STATES = frozenset({GoalStepStatus.DONE.value, GoalStepStatus.SKIPPED.value})


def timezone_for(name: str) -> timezone | Any:
    """返回时区对象；缺少 tzdata 时回退到 UTC，避免计划读取因环境差异失败。"""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception:  # noqa: BLE001 - 时区数据缺失时退化为 UTC
        return timezone.utc


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    parsed = parse_datetime(value)
    if parsed is not None:
        return parsed.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


class GoalSchedule(BaseModel):
    """步骤触发规则：一次性时间点，或每周固定时刻（可带截止日期）。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    kind: Literal["once", "weekly"] = "once"
    at: str | None = None
    weekday: int | None = Field(default=None, ge=0, le=6)
    time: str | None = None
    timezone: str = DEFAULT_TIMEZONE
    until: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "GoalSchedule":
        if self.kind == "once":
            if parse_datetime(self.at) is None:
                raise ValueError("once schedule requires ISO8601 at")
        else:
            if self.weekday is None or not self.time:
                raise ValueError("weekly schedule requires weekday and time")
            try:
                dtime.fromisoformat(self.time)
            except ValueError as error:
                raise ValueError("weekly schedule time must be HH:MM") from error
            if self.until is not None and parse_date(self.until) is None:
                raise ValueError("until must be a date")
        return self


def next_occurrence(schedule: GoalSchedule | None, after: datetime) -> datetime | None:
    """旧版 schedule 的下一次触发时间；统一委托给 TriggerRule 的排期实现。

    日历计算只保留 next_time_trigger 一处，避免 once/weekly 两套实现漂移。
    """
    if schedule is None:
        return None
    if schedule.kind == "once":
        if parse_datetime(schedule.at) is None:
            return None
    elif schedule.weekday is None or not schedule.time:
        return None
    return next_time_trigger(trigger_from_schedule(schedule), after)


class TriggerKind(StrEnum):
    """步骤触发规则的五种形态（与 GoalSchedule 并存，schedule 视为旧写法）。"""

    ONCE_AT = "once_at"
    RECURRING = "recurring"
    WINDOW_ONCE = "window_once"
    EVENT_CONDITION = "event_condition"
    OPPORTUNISTIC = "opportunistic"


class TriggerRule(BaseModel):
    """单个步骤的独立触发规则。

    与 GoalSchedule 的区别：schedule 只表达"一次性/每周"的时间点，
    TriggerRule 额外支持窗口条件、事件条件与受冷却约束的不定期跟进。
    schedule 保留用于向后兼容，读取时由 trigger_of 统一转换。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    kind: TriggerKind
    # once_at / window_once
    at: str | None = None
    start: str | None = None
    end: str | None = None
    # recurring
    frequency: Literal["daily", "weekly"] = "weekly"
    weekday: int | None = Field(default=None, ge=0, le=6)
    time: str | None = None
    timezone: str = DEFAULT_TIMEZONE
    until: str | None = None
    # event_condition / opportunistic（window_once 也用它表达"条件"）
    event_types: list[str] = Field(default_factory=list, max_length=16)
    match: dict[str, Any] = Field(default_factory=dict)
    # opportunistic：不定期跟进必须受冷却与频率上限约束，禁止无条件周期调用。
    cooldown_hours: int = Field(default=24, ge=1, le=24 * 30)
    max_per_day: int = Field(default=2, ge=1, le=50)
    max_per_week: int = Field(default=5, ge=1, le=100)
    # 允许把"某一步完成"当作评估时机（仍受冷却/上限约束）。
    evaluate_on_step_done: bool = True

    @model_validator(mode="after")
    def validate_shape(self) -> "TriggerRule":
        if self.kind == TriggerKind.ONCE_AT:
            if parse_datetime(self.at) is None:
                raise ValueError("once_at requires ISO8601 at")
        elif self.kind == TriggerKind.RECURRING:
            if not self.time:
                raise ValueError("recurring requires time (HH:MM)")
            try:
                dtime.fromisoformat(self.time)
            except ValueError as error:
                raise ValueError("recurring time must be HH:MM") from error
            if self.frequency == "weekly" and self.weekday is None:
                raise ValueError("weekly recurring requires weekday")
            if self.until is not None and parse_date(self.until) is None:
                raise ValueError("until must be a date")
        elif self.kind == TriggerKind.WINDOW_ONCE:
            start = parse_datetime(self.start)
            end = parse_datetime(self.end)
            if start is None or end is None:
                raise ValueError("window_once requires start and end")
            if start >= end:
                raise ValueError("window_once start must be before end")
        elif self.kind == TriggerKind.EVENT_CONDITION:
            if not self.event_types:
                raise ValueError("event_condition requires event_types")
        elif self.kind == TriggerKind.OPPORTUNISTIC:
            if not self.event_types and not self.evaluate_on_step_done:
                raise ValueError("opportunistic requires event_types or evaluate_on_step_done")
        return self


def trigger_from_schedule(schedule: GoalSchedule | None) -> TriggerRule | None:
    """把旧版 GoalSchedule 归一化成 TriggerRule；无计划返回 None。"""
    if schedule is None:
        return None
    if schedule.kind == "once":
        return TriggerRule(kind=TriggerKind.ONCE_AT, at=schedule.at, timezone=schedule.timezone)
    return TriggerRule(
        kind=TriggerKind.RECURRING,
        frequency="weekly",
        weekday=schedule.weekday,
        time=schedule.time,
        timezone=schedule.timezone,
        until=schedule.until,
    )


def trigger_of(step: GoalStep) -> TriggerRule | None:
    """返回步骤的生效触发规则：显式 trigger 优先，其次兼容旧 schedule。"""
    return step.trigger if step.trigger is not None else trigger_from_schedule(step.schedule)


def next_time_trigger(rule: TriggerRule | None, after: datetime) -> datetime | None:
    """计算时间型规则在 after 之后的下一次触发；事件型规则返回 None。"""
    if rule is None:
        return None
    zone = timezone_for(rule.timezone)
    moment = after.astimezone(zone)
    if rule.kind == TriggerKind.ONCE_AT:
        target = parse_datetime(rule.at)
        return target if target is not None and target > moment else None
    if rule.kind == TriggerKind.WINDOW_ONCE:
        start = parse_datetime(rule.start)
        end = parse_datetime(rule.end)
        if start is None or end is None:
            return None
        # 窗口内由扫描器按"条件是否满足"决定，不产生固定时间点。
        return start if moment < start else None
    if rule.kind == TriggerKind.RECURRING:
        clock = dtime.fromisoformat(rule.time or "00:00")
        candidate = datetime.combine(moment.date(), clock, tzinfo=zone)
        if rule.frequency == "weekly":
            delta = ((rule.weekday if rule.weekday is not None else 0) - moment.weekday()) % 7
            candidate = candidate + timedelta(days=delta)
        if candidate <= moment:
            candidate = candidate + timedelta(days=7 if rule.frequency == "weekly" else 1)
        until = parse_date(rule.until)
        if until is not None and candidate.date() > until:
            return None
        return candidate
    return None


class GoalStep(BaseModel):
    """目标的一个可独立推进步骤；id 在目标内稳定，状态与证据随执行更新。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=200)
    kind: GoalStepKind = GoalStepKind.AUTONOMOUS
    status: GoalStepStatus = GoalStepStatus.PENDING
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_STEPS)
    acceptance: list[str] = Field(default_factory=list, max_length=32)
    schedule: GoalSchedule | None = None
    requires_evidence: bool = True
    evidence_refs: list[str] = Field(default_factory=list, max_length=128)
    attempts: int = Field(default=0, ge=0, le=100)
    missed_count: int = Field(default=0, ge=0, le=1000)
    next_trigger_at: str | None = None
    last_occurrence: str | None = None
    last_result: dict[str, Any] | None = None
    note: str = Field(default="", max_length=2000)
    # approval_required 步骤的人工审批时间；为空表示尚未审批。
    approved_at: str | None = None

    # 触发规则（V50）：显式 trigger 优先，schedule 仅作旧写法兼容。
    trigger: TriggerRule | None = None
    # 触发状态：幂等键、次数、冷却与频率窗口，全部随计划持久化，重启可恢复。
    trigger_count: int = Field(default=0, ge=0, le=10000)
    last_trigger_key: str = Field(default="", max_length=128)
    last_trigger_at: str | None = None
    armed_at: str | None = None
    expired_at: str | None = None
    cooldown_until: str | None = None
    opportunistic_hits: list[str] = Field(default_factory=list, max_length=200)
    last_message_key: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def validate_step(self) -> "GoalStep":
        if self.id in self.depends_on:
            raise ValueError("step cannot depend on itself")
        # 交互步骤的"完成"由用户实际参与决定，不要求机器证据；
        # 自主步骤默认必须带 Evidence，避免 LLM 口头宣称成功就推进目标。
        if self.kind == GoalStepKind.INTERACTIVE:
            self.requires_evidence = False
        return self


class GoalPlan(BaseModel):
    """长期目标计划：目标级状态 + 步骤集合 + 提醒/错过记录。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    status: GoalStatus = GoalStatus.PROPOSED
    summary: str = Field(default="", max_length=2000)
    deadline: str | None = None
    timezone: str = DEFAULT_TIMEZONE
    acceptance: list[str] = Field(default_factory=list, max_length=32)
    steps: list[GoalStep] = Field(min_length=1, max_length=MAX_STEPS)
    current_step_id: str | None = None
    created_at: str = ""
    confirmed_at: str | None = None
    last_progress_at: str | None = None
    reminder: dict[str, Any] = Field(default_factory=dict)
    missed_streak: int = Field(default=0, ge=0, le=1000)
    revision: int = Field(default=1, ge=1)
    blocked_reason: str = ""

    @model_validator(mode="after")
    def validate_graph(self) -> "GoalPlan":
        ids = [step.id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate step id")
        known = set(ids)
        for step in self.steps:
            missing = [dep for dep in step.depends_on if dep not in known]
            if missing:
                raise ValueError(f"unknown dependency: {missing}")
        # 拓扑检查：依赖图必须无环，否则推进永远无法收敛。
        visiting: set[str] = set()
        visited: set[str] = set()
        graph = {step.id: list(step.depends_on) for step in self.steps}

        def _walk(node: str) -> None:
            if node in visited:
                return
            if node in visiting:
                raise ValueError("dependency cycle detected")
            visiting.add(node)
            for dep in graph.get(node, []):
                _walk(dep)
            visiting.discard(node)
            visited.add(node)

        for step_id in graph:
            _walk(step_id)
        if self.current_step_id is not None and self.current_step_id not in known:
            self.current_step_id = None
        return self

    def step(self, step_id: str) -> GoalStep | None:
        for item in self.steps:
            if item.id == step_id:
                return item
        return None

    def current_step(self) -> GoalStep | None:
        return self.step(self.current_step_id) if self.current_step_id else None

    def progress(self) -> dict[str, int]:
        counts = {"total": len(self.steps), "done": 0, "failed": 0, "pending": 0, "waiting": 0}
        for item in self.steps:
            if item.status in FINISHED_STEP_STATES:
                counts["done"] += 1
            elif item.status == GoalStepStatus.FAILED.value:
                counts["failed"] += 1
            elif item.status in {
                GoalStepStatus.WAITING_USER.value, GoalStepStatus.WAITING_TIME.value,
                GoalStepStatus.RUNNING.value, GoalStepStatus.READY.value,
            }:
                counts["waiting"] += 1
            else:
                counts["pending"] += 1
        return counts


def dependencies_met(plan: GoalPlan, step: GoalStep) -> bool:
    for dep_id in step.depends_on:
        dep = plan.step(dep_id)
        if dep is None or dep.status not in FINISHED_STEP_STATES:
            return False
    return True


def next_runnable_step(plan: GoalPlan, now: datetime) -> GoalStep | None:
    """返回当前可以立即执行的步骤：依赖满足、未完成、且已到触发时间。

    已置为 READY 的步骤表示触发已发生（定时到期或用户点击开始），此时即使
    ``next_trigger_at`` 已滚动到下一次周期，也必须视为可执行，否则本轮唤醒会空转。
    """
    for step in plan.steps:
        if step.status not in {GoalStepStatus.PENDING.value, GoalStepStatus.READY.value}:
            continue
        if not dependencies_met(plan, step):
            continue
        if step.status == GoalStepStatus.READY.value:
            return step
        # 交互步骤必须由用户点击开始（置为 READY）后才可执行，不能自动起跑。
        if step.kind == GoalStepKind.INTERACTIVE.value:
            continue
        # 显式触发规则（V50）只由 GoalTriggerService 命中后置 READY，
        # 这里不能按时间自动起跑，否则 window_once / event_condition 会被绕过。
        if step.trigger is not None:
            continue
        if step.schedule is not None:
            due = parse_datetime(step.next_trigger_at)
            if due is None or due > now:
                continue
        return step
    return None


def upcoming_steps(plan: GoalPlan) -> list[GoalStep]:
    return [
        step for step in plan.steps
        if step.status in {GoalStepStatus.PENDING.value, GoalStepStatus.WAITING_TIME.value}
        and trigger_of(step) is not None
        and step.next_trigger_at is not None
    ]


def plan_complete(plan: GoalPlan) -> bool:
    return all(step.status in FINISHED_STEP_STATES for step in plan.steps)


def summarize(plan: GoalPlan) -> dict[str, Any]:
    current = plan.current_step()
    upcoming = upcoming_steps(plan)
    triggers = [
        moment
        for moment in (parse_datetime(step.next_trigger_at) for step in upcoming)
        if moment is not None
    ]
    next_trigger = min(triggers, default=None)
    return {
        "status": plan.status.value if hasattr(plan.status, "value") else plan.status,
        "summary": plan.summary,
        "deadline": plan.deadline,
        "progress": plan.progress(),
        "current_step": _step_brief(current) if current else None,
        "next_trigger_at": next_trigger.isoformat() if next_trigger else None,
        "blocked_reason": plan.blocked_reason,
        "steps": [_step_brief(step) for step in plan.steps],
    }


def _step_brief(step: GoalStep) -> dict[str, Any]:
    return {
        "id": step.id,
        "title": step.title,
        "kind": step.kind.value if hasattr(step.kind, "value") else step.kind,
        "status": step.status.value if hasattr(step.status, "value") else step.status,
        "depends_on": list(step.depends_on),
        "acceptance": list(step.acceptance),
        "next_trigger_at": step.next_trigger_at,
        "trigger": (
            step.trigger.kind.value if step.trigger is not None else None
        ),
        "trigger_count": step.trigger_count,
        "evidence_count": len(step.evidence_refs),
        "missed_count": step.missed_count,
        "note": step.note,
    }
