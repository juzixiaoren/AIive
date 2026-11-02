"""待回答问题的生命周期：主动提问 → 用户回复 → 关联 → 关闭。

设计约束（V58）：
- 复用 `agent_task_events` 表达生命周期，不新增表：
  `goal_question_asked`（提问）/ `goal_question_resolved`（已处理）/ `goal_question_expired`（过期标记）；
- question_id 由 (task_id, step_id, trigger_key) 确定性派生，Outbox 重投/进程重启
  不会产生第二个问题，也不会把同一问题的回答应用两次；
- 状态是**推导值**（有 resolution → resolved；超过 expires_at → expired；
  任务已终结 / 目标暂停或结束 / 步骤不存在或已不再等待用户 → stale），
  因此过期与失效都不需要独立调度器，也不会在恢复目标后误判；
- 失效问题的状态同样从既有 task_state["goal"] 推导：目标恢复为 active 且步骤仍
  在 waiting_user 时问题自动重新有效，因此暂停期间不需要写任何标记事件；
- 过期后一律不允许改目标，只记录标记，提醒与沉默继续沿用 V50 的既有策略。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import AgentTask, AgentTaskEvent
from aiive.task_runtime.goal_plan import GoalPlan, GoalStatus, GoalStepStatus
from aiive.task_runtime.repository import TERMINAL_TASK_STATES, TaskRepository, utcnow

logger = logging.getLogger(__name__)

QUESTION_ASKED_EVENT = "goal_question_asked"
QUESTION_RESOLVED_EVENT = "goal_question_resolved"
QUESTION_EXPIRED_EVENT = "goal_question_expired"

# 问题有效期：超过后不再允许通过回复修改目标（不新建调度器，状态在读取时推导）。
QUESTION_TTL_HOURS = 72
MAX_OPEN_QUESTIONS = 5
MAX_QUESTION_CHARS = 800
MAX_ANSWER_CHARS = 2000

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
STATUS_EXPIRED = "expired"
STATUS_STALE = "stale"

# 失效原因（同样是读取时推导的派生值，不落库）：让调用方能明确知道问题为什么
# 不再可回答，而不是只看到一个笼统的 stale。
INVALID_TASK_TERMINAL = "task_terminal"
INVALID_GOAL_MISSING = "goal_not_found"
INVALID_STEP_MISSING = "step_not_found"
INVALID_STEP_NOT_WAITING = "step_not_waiting_user"
INVALID_SUPERSEDED = "superseded"

INTENTS = frozenset({"answer", "modify_plan", "reject"})


@dataclass(frozen=True)
class PendingQuestion:
    """一个待回答问题的只读视图（状态由事件与时间推导）。"""

    question_id: str
    task_id: str
    thread_id: str
    step_id: str
    decision_id: str
    trigger_key: str
    question: str
    message_event_id: str
    created_at: datetime
    expires_at: datetime
    status: str = STATUS_OPEN
    answer: str = ""
    intent: str = ""
    user_event_id: str = ""
    task_status: str = ""
    # 仅当 status=stale 时有意义：说明目标/步骤已经不在可回答的状态上。
    invalid_reason: str = ""

    @property
    def is_open(self) -> bool:
        return self.status == STATUS_OPEN


def question_id_for(task_id: str, step_id: str, trigger_key: str) -> str:
    """稳定问题标识：同一触发只可能对应一个问题。"""
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"goal_question:{task_id}:{step_id}:{trigger_key}",
    ))


def _asked_key(question_id: str) -> str:
    return f"goal_question:{question_id}"


def _resolved_key(question_id: str) -> str:
    return f"goal_question_resolved:{question_id}"


def _expired_key(question_id: str) -> str:
    return f"goal_question_expired:{question_id}"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str) and value:
        try:
            return _as_utc(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


def record_question(
    db: Session,
    *,
    task_id: str,
    step_id: str,
    decision_id: str,
    trigger_key: str,
    question: str,
    message_event_id: str = "",
    ttl_hours: int = QUESTION_TTL_HOURS,
) -> str:
    """在决策应用事务内登记问题（幂等）；调用方负责 commit。"""
    question_id = question_id_for(task_id, step_id, trigger_key)
    now = utcnow()
    TaskRepository(db).append_event(
        task_id,
        QUESTION_ASKED_EVENT,
        {
            "question_id": question_id,
            "step_id": step_id,
            "decision_id": decision_id,
            "trigger_key": trigger_key,
            "question": (question or "").strip()[:MAX_QUESTION_CHARS],
            "message_event_id": message_event_id,
            "asked_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=ttl_hours)).isoformat(),
        },
        visibility="internal",
        dedupe_key=_asked_key(question_id),
    )
    return question_id


def _asked_rows(db: Session, *, task_id: str | None = None, thread_id: str | None = None,
                since: datetime | None = None) -> list[tuple[AgentTaskEvent, AgentTask]]:
    query = db.query(AgentTaskEvent, AgentTask).join(
        AgentTask, AgentTask.id == AgentTaskEvent.task_id,
    ).filter(AgentTaskEvent.event_type == QUESTION_ASKED_EVENT)
    if task_id:
        query = query.filter(AgentTaskEvent.task_id == task_id)
    if thread_id:
        query = query.filter(AgentTask.thread_id == thread_id)
    if since is not None:
        query = query.filter(AgentTaskEvent.created_at >= since)
    return list(query.order_by(AgentTaskEvent.created_at.asc()).all())


def _resolution_rows(db: Session, question_ids: list[str]) -> tuple[dict[str, AgentTaskEvent], set[str]]:
    if not question_ids:
        return {}, set()
    keys = [_resolved_key(item) for item in question_ids]
    resolved = {
        str((row.payload or {}).get("question_id") or ""): row
        for row in db.query(AgentTaskEvent).filter(AgentTaskEvent.dedupe_key.in_(keys)).all()
    }
    expired_keys = {_expired_key(item) for item in question_ids}
    expired = {
        str((row.payload or {}).get("question_id") or "")
        for row in db.query(AgentTaskEvent).filter(AgentTaskEvent.dedupe_key.in_(expired_keys)).all()
    }
    return resolved, expired


def _plan_snapshot(task: AgentTask) -> tuple[str, dict[str, str]]:
    """读取目标的真实状态与各步骤状态（计划缺失/损坏时返回空 → 问题一律失效）。

    目标暂停、结束，或步骤被修改/重置后，历史问题不能再被当作可回答问题注入，
    这些状态全部来自既有的 task_state["goal"]，不需要新增表或事件。
    """
    raw = (task.task_state or {}).get("goal")
    if not isinstance(raw, dict):
        return "", {}
    try:
        plan = GoalPlan.model_validate(raw)
    except Exception:  # noqa: BLE001 - 计划被写坏时按失效处理，不影响其它线程
        logger.warning("目标计划解析失败，待回答问题按失效处理: task_id=%s", task.id, exc_info=True)
        return "", {}
    return str(plan.status), {str(step.id): str(step.status) for step in plan.steps}


def _superseded_ids(rows: list[tuple[AgentTaskEvent, AgentTask]]) -> set[str]:
    """同一 (task, step) 只保留最后一次提问：更早的问题被新提问取代。

    步骤被重置后重新提问会生成新的 question_id；若不收敛，旧问题会在 72h 内
    一直挂在上下文里，用户回答它只会被服务端拒绝。
    """
    latest: dict[tuple[str, str], tuple[datetime, str]] = {}
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    for event, task in rows:
        payload = dict(event.payload or {})
        question_id = str(payload.get("question_id") or "")
        if not question_id:
            continue
        asked_at = _parse_dt(payload.get("asked_at")) or _as_utc(event.created_at) or epoch
        key = (str(task.id), str(payload.get("step_id") or ""))
        current = latest.get(key)
        if current is None or asked_at >= current[0]:
            latest[key] = (asked_at, question_id)
    kept = {question_id for _asked_at, question_id in latest.values()}
    return {
        str(dict(event.payload or {}).get("question_id") or "")
        for event, _task in rows
    } - kept - {""}


def _invalid_reason(
    task: AgentTask,
    *,
    plan_status: str,
    step_statuses: dict[str, str],
    step_id: str,
    question_id: str,
    superseded: set[str],
) -> str:
    """问题当前是否还有效：空串表示仍可回答，否则返回明确的失效原因。"""
    if str(task.status) in TERMINAL_TASK_STATES:
        return INVALID_TASK_TERMINAL
    if not plan_status:
        return INVALID_GOAL_MISSING
    if plan_status != GoalStatus.ACTIVE.value:
        # 暂停/阻塞/已结束：计划状态变化后旧问题不再进上下文；恢复后自动重新有效。
        return f"goal_{plan_status}"
    if not step_id or step_id not in step_statuses:
        return INVALID_STEP_MISSING
    if step_statuses[step_id] != GoalStepStatus.WAITING_USER.value:
        return INVALID_STEP_NOT_WAITING
    if question_id in superseded:
        return INVALID_SUPERSEDED
    return ""


def _to_question(
    event: AgentTaskEvent,
    task: AgentTask,
    resolved: dict[str, AgentTaskEvent],
    expired: set[str],
    now: datetime,
    *,
    plan_status: str = "",
    step_statuses: dict[str, str] | None = None,
    superseded: set[str] | None = None,
) -> PendingQuestion:
    payload = dict(event.payload or {})
    question_id = str(payload.get("question_id") or event.dedupe_key or "").replace(
        "goal_question:", "", 1,
    )
    created_at = _parse_dt(payload.get("asked_at")) or _as_utc(event.created_at) or now
    expires_at = _parse_dt(payload.get("expires_at")) or (created_at + timedelta(hours=QUESTION_TTL_HOURS))
    status = STATUS_OPEN
    invalid_reason = ""
    answer = ""
    intent = ""
    user_event_id = ""
    resolution = resolved.get(question_id)
    step_id = str(payload.get("step_id") or "")
    if resolution is not None:
        status = STATUS_RESOLVED
        resolution_payload = dict(resolution.payload or {})
        answer = str(resolution_payload.get("answer") or "")
        intent = str(resolution_payload.get("intent") or "")
        user_event_id = str(resolution_payload.get("user_event_id") or "")
    elif question_id in expired or now >= expires_at:
        status = STATUS_EXPIRED
    else:
        # 已回答/已过期之外，还要核实目标与步骤是否仍允许回答。
        invalid_reason = _invalid_reason(
            task,
            plan_status=plan_status,
            step_statuses=step_statuses or {},
            step_id=step_id,
            question_id=question_id,
            superseded=superseded or set(),
        )
        if invalid_reason:
            status = STATUS_STALE
    return PendingQuestion(
        question_id=question_id,
        task_id=str(task.id),
        thread_id=str(task.thread_id),
        step_id=step_id,
        decision_id=str(payload.get("decision_id") or ""),
        trigger_key=str(payload.get("trigger_key") or ""),
        question=str(payload.get("question") or ""),
        message_event_id=str(payload.get("message_event_id") or ""),
        created_at=created_at,
        expires_at=expires_at,
        status=status,
        answer=answer,
        intent=intent,
        user_event_id=user_event_id,
        task_status=str(task.status),
        invalid_reason=invalid_reason,
    )


def list_open_questions(
    db: Session, thread_id: str, *, now: datetime | None = None,
    limit: int = MAX_OPEN_QUESTIONS,
) -> list[PendingQuestion]:
    """同一线程内仍可回答的问题（按提问时间升序，只返回最近 limit 条）。"""
    if not thread_id:
        return []
    moment = _as_utc(now) or utcnow()
    cutoff = moment - timedelta(hours=QUESTION_TTL_HOURS)
    rows = _asked_rows(db, thread_id=thread_id, since=cutoff)
    if not rows:
        return []
    question_ids = []
    for event, _task in rows:
        payload = dict(event.payload or {})
        question_id = str(payload.get("question_id") or "")
        if question_id:
            question_ids.append(question_id)
    resolved, expired = _resolution_rows(db, question_ids)
    superseded = _superseded_ids(rows)
    snapshots: dict[str, tuple[str, dict[str, str]]] = {}
    open_questions: list[PendingQuestion] = []
    for event, task in rows:
        snapshot = snapshots.get(str(task.id))
        if snapshot is None:
            snapshot = _plan_snapshot(task)
            snapshots[str(task.id)] = snapshot
        item = _to_question(
            event, task, resolved, expired, moment,
            plan_status=snapshot[0], step_statuses=snapshot[1], superseded=superseded,
        )
        if item.is_open:
            open_questions.append(item)
    return open_questions[-limit:]


def load_question(db: Session, question_id: str, *, now: datetime | None = None) -> PendingQuestion | None:
    """按 question_id 读取（含已关闭状态，用于幂等判定与排障）。"""
    if not question_id:
        return None
    row = db.query(AgentTaskEvent).filter(
        AgentTaskEvent.dedupe_key == _asked_key(question_id),
    ).one_or_none()
    if row is None:
        return None
    task = db.get(AgentTask, row.task_id)
    if task is None:
        return None
    moment = _as_utc(now) or utcnow()
    resolved, expired = _resolution_rows(db, [question_id])
    # 同一 (task, step) 的后续提问会让本问题失效：需要连同该任务近期的问题一起看。
    recent = _asked_rows(
        db, task_id=str(row.task_id), since=moment - timedelta(hours=QUESTION_TTL_HOURS),
    )
    plan_status, step_statuses = _plan_snapshot(task)
    return _to_question(
        row, task, resolved, expired, moment,
        plan_status=plan_status,
        step_statuses=step_statuses,
        superseded=_superseded_ids(recent or [(row, task)]),
    )


def resolve_question(
    db: Session,
    *,
    question: PendingQuestion,
    user_event_id: str,
    answer: str,
    intent: str,
    outcome: dict[str, Any] | None = None,
) -> bool:
    """关闭问题（幂等）；返回是否由本次调用写入。调用方负责 commit。"""
    existing = db.query(AgentTaskEvent.id).filter(
        AgentTaskEvent.dedupe_key == _resolved_key(question.question_id),
    ).first()
    if existing is not None:
        return False
    TaskRepository(db).append_event(
        question.task_id,
        QUESTION_RESOLVED_EVENT,
        {
            "question_id": question.question_id,
            "step_id": question.step_id,
            "user_event_id": user_event_id,
            "answer": (answer or "")[:MAX_ANSWER_CHARS],
            "intent": intent,
            "outcome": dict(outcome or {}),
            "resolved_at": utcnow().isoformat(),
        },
        visibility="internal",
        dedupe_key=_resolved_key(question.question_id),
    )
    return True


def mark_expired(db: Session, question: PendingQuestion) -> None:
    """记录过期标记（幂等），用于审计；不改动任何计划状态。"""
    TaskRepository(db).append_event(
        question.task_id,
        QUESTION_EXPIRED_EVENT,
        {
            "question_id": question.question_id,
            "step_id": question.step_id,
            "expired_at": utcnow().isoformat(),
        },
        visibility="internal",
        dedupe_key=_expired_key(question.question_id),
    )


def render_questions_block(questions: list[PendingQuestion], *, limit_chars: int = 1200) -> str:
    """把待回答问题渲染成上下文块；为空时返回空串（不占用预算）。"""
    if not questions:
        return ""
    lines = [
        "## 待回答的问题",
        "你在之前主动向用户提出了下列问题，用户可能正在回复其中某一个。",
        "处理规则：",
        "1. 如果当前消息明确回答了其中某个问题，调用 answer_goal_question 并传入该 question_id；",
        "2. 如果用户是在修改计划，intent 用 modify_plan 并给出 plan_changes；明确拒绝则用 reject；",
        "3. 如果无法判断用户在回答哪一个问题，先用一句话向用户澄清，不要猜测、不要随意绑定；",
        "4. 如果只是普通聊天，不要调用该工具，正常回答即可。",
    ]
    for item in questions:
        asked_at = item.created_at.isoformat()
        expires_at = item.expires_at.isoformat()
        question_text = item.question or "(未记录)"
        lines.append(
            f"- question_id={item.question_id}｜任务「{item.task_id}」"
            + f"步骤「{item.step_id}」｜问题：{question_text}"
            + f"｜提出于 {asked_at}，有效至 {expires_at}",
        )
    text = "\n".join(lines)
    if len(text) > limit_chars:
        text = text[:limit_chars] + "…"
    return text
