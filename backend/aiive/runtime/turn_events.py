"""Outbox 持久投递运行事件，Inbox 在工具调用配对后把事件作为 SystemMessage 交给 Turn。

Goal 事件由现有决策流程兜底。任务终态可在空闲时入箱，重新领取前按 Turn 租约恢复，
只有本 Turn 对应的状态读取回执和决策都成立时才随回复收口。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from aiive.db.models import Event, OutboxJob, SelfDevRequest, TurnEventInbox, TurnRecord
from aiive.db.forget_models import ContentProvenanceRef
from aiive.forget.durable_sources import collect_sources, persist_sources, references_visible, sources_visible
from aiive.prompts import get_prompt_registry
from aiive.task_runtime.read_repository import TaskReadRepository, inherit_sources
from aiive.worker.job_types import MAIN_AGENT_TASK_TERMINAL

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_INJECTED = "injected"
STATUS_DEFERRED = "deferred"
STATUS_HANDLED = "handled"
STATUS_EXPIRED = "expired"

OPEN_STATUSES = (STATUS_PENDING, STATUS_INJECTED, STATUS_DEFERRED)

# 收件箱溢出淘汰的可识别处置原因：写入 decision 列（该行不会再有模型决策），
# 使排障与 Outbox 兜底路径能区分“Turn 内已处理”与“被溢出淘汰”。
DECISION_EVICTED_OVERFLOW = "evicted_overflow"

# 单次注入上限：超出部分留在收件箱，等下一次 assistant 调用再合并，避免一次
# 注入无限膨胀；同时限制线程级积压总量，防止 Outbox 重试风暴造成无界增长。
MAX_INJECT_PER_CALL = 5
MAX_PENDING_PER_THREAD = 50

INTERNAL_CONTEXT_HEADER = "[内部事件上下文 · 非用户消息]"

# 决策标记协议的唯一事实源：正则清洗（最终持久化文本）与流式过滤器必须共用同一套
# 边界，否则"流里已经展示、落库却已剥离"会产出两份不一致的用户可见文本。
# 空白段限长让合法标记长度有上界，流式扫描才能在有限缓冲内确定性判定，
# 而不是为了等一个可能永不闭合的标记无限吞字。
DECISION_MARKER_WS_MAX = 8
DECISION_DECISIONS = ("act_now", "defer", "continue", "no_action")
DECISION_MARKER_MAX_LEN = (
    2  # [[
    + DECISION_MARKER_WS_MAX
    + len("EVENT_DECISION")
    + DECISION_MARKER_WS_MAX
    + 1  # :
    + DECISION_MARKER_WS_MAX
    + max(len(word) for word in DECISION_DECISIONS)
    + DECISION_MARKER_WS_MAX
    + 2  # ]]
)
DECISION_MARKER_RE = re.compile(
    r"\[\[\s{{0,{ws}}}EVENT_DECISION\s{{0,{ws}}}:\s{{0,{ws}}}({decisions})\s{{0,{ws}}}\]\]".format(
        ws=DECISION_MARKER_WS_MAX,
        decisions="|".join(DECISION_DECISIONS),
    ),
    re.IGNORECASE,
)

MARKER_HEAD_WORD = "event_decision"
MARKER_CLOSE = "]]"


def scan_decision_marker(text: str) -> tuple[str, int]:
    """扫描 text 开头的一个决策标记，返回 (状态, 结束下标)。

    状态取值：
    - `match`：完整合法标记，结束下标为其后一位，整段应剥离；
    - `partial`：当前内容是某个合法标记的严格前缀，需要更多字符才能判定；
    - `none`：确定不是标记，应按普通文本处理。

    合法标记长度不超过 `DECISION_MARKER_MAX_LEN`，因此 partial 状态的缓冲
    天然有界——调用方可以据此给出确定性的等待/放弃策略。
    """
    length = len(text)
    if not text.startswith("[["):
        return ("none", 0)
    pos = 2

    def literal(word: str) -> str:
        nonlocal pos
        remaining = length - pos
        for offset in range(min(len(word), remaining)):
            if text[pos + offset].lower() != word[offset]:
                return "none"
        if remaining < len(word):
            return "partial"
        pos += len(word)
        return "ok"

    def space() -> str:
        nonlocal pos
        start = pos
        while pos < length and text[pos].isspace():
            pos += 1
            if pos - start > DECISION_MARKER_WS_MAX:
                return "none"
        return "partial" if pos >= length else "ok"

    def decision() -> str:
        nonlocal pos
        tail = text[pos:].lower()
        matched = [word for word in DECISION_DECISIONS if tail.startswith(word)]
        if matched:
            pos += max(len(word) for word in matched)
            return "ok"
        if any(word.startswith(tail) for word in DECISION_DECISIONS):
            return "partial"
        return "none"

    steps = (
        space,
        lambda: literal(MARKER_HEAD_WORD),
        space,
        lambda: literal(":"),
        space,
        decision,
        space,
        lambda: literal(MARKER_CLOSE),
    )
    for step in steps:
        status = step()
        if status != "ok":
            return (status, 0)
    return ("match", pos)


class DecisionMarkerStreamFilter:
    """流式剥离内部决策标记，输出与 `remove_decision_markers` 逐字一致。

    设计要点：
    - 安全文本立即透传，缓冲尾部最多滞留一个 `[`，用于等待跨分片的 `[[`；
    - 命中完整标记即整段丢弃；未完成标记只在长度不超过
      `DECISION_MARKER_MAX_LEN` 时继续等待，超过即判定为普通文本，
      因此缓冲始终有界，不会因为模型输出了半个标记而无限吞字；
    - 非法片段与"流结束时仍未闭合"的片段原样输出，与正则清洗结果一致；
    - 只有当标记出现在首个非空白字符之前时才丢弃其前导空白，
      与 `remove_decision_markers` 的 lstrip 规则相同，保证流式累计文本
      与最终持久化文本逐字相同。
    """

    def __init__(self) -> None:
        self._buffer: str = ""
        self._lead: str = ""
        self._emitted: bool = False
        self._dropped_lead: bool = False

    @property
    def pending_chars(self) -> int:
        """当前仍在等待判定的字符数（有界性断言与排障用）。"""
        return len(self._buffer)

    def feed(self, text: str) -> str:
        """喂入一个流式分片，返回可立即透传给用户的文本。"""
        if text:
            self._buffer += text
        return self._drain(final=False)

    def flush(self) -> str:
        """流结束时收尾：未完成标记按普通文本输出，暂存的前导空白按需保留。"""
        tail = self._drain(final=True)
        if self._emitted or not self._lead:
            return tail
        lead = self._lead
        self._lead = ""
        self._emitted = True
        return tail + lead

    def _drain(self, *, final: bool) -> str:
        out: list[str] = []
        while True:
            buffer = self._buffer
            if not buffer:
                break
            index = buffer.find("[[")
            if index < 0:
                if final:
                    out.append(buffer)
                    self._buffer = ""
                    break
                keep = 1 if buffer.endswith("[") else 0
                if keep:
                    out.append(buffer[:-1])
                    self._buffer = buffer[-1:]
                else:
                    out.append(buffer)
                    self._buffer = ""
                break
            if index > 0:
                out.append(buffer[:index])
                self._buffer = buffer[index:]
                continue
            status, end = scan_decision_marker(buffer)
            if status == "match":
                if not self._emitted:
                    self._dropped_lead = True
                    self._lead = ""
                self._buffer = buffer[end:]
                continue
            if status == "partial" and not final and len(buffer) <= DECISION_MARKER_MAX_LEN:
                break
            out.append(buffer[:1])
            self._buffer = buffer[1:]
        return self._emit("".join(out))

    def _emit(self, text: str) -> str:
        if not text:
            return ""
        if self._emitted:
            return text
        if not text.strip():
            if not self._dropped_lead:
                self._lead += text
            return ""
        self._emitted = True
        if self._dropped_lead:
            self._lead = ""
            return text.lstrip()
        lead = self._lead
        self._lead = ""
        return lead + text


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bounded(text: Any, limit: int) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit] + "…"


def active_turn_for_thread(db: Session, thread_id: str) -> TurnRecord | None:
    """返回该线程当前持有有效租约的前台 Turn；没有则 None。"""
    if not thread_id:
        return None
    return db.query(TurnRecord).filter(
        TurnRecord.thread_id == thread_id,
        TurnRecord.status == "running",
        TurnRecord.lease_expires_at.is_not(None),
        TurnRecord.lease_expires_at > _now(),
    ).order_by(TurnRecord.turn_sequence.desc()).first()


def trigger_event_sources(
    db: Session, event_id: str, event_type: str, payload: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[tuple[str, str]]] | None:
    """Resolve existing trigger types without treating domain IDs as chat Event IDs."""
    from aiive.forget.source_policy import SourceReferences, blocked_sources_reason
    from aiive.task_runtime.goal_events import SUPPORTED_EVENT_TYPES

    reader = TaskReadRepository(db)
    parents: list[tuple[str, str]] = []
    refs: set[tuple[str, str]] = set()
    event = db.get(Event, event_id) if event_id else None
    if not event_id:
        # Timers have no Event body; explicit source fields in other bodies still matter.
        try:
            collect_sources(payload, refs)
        except ValueError:
            return None
        if payload and not refs:
            return None
    elif event is not None:
        refs.add(("event", event.id))
    elif event_id.startswith("step-done:"):
        step_event = reader.get_event(event_id.removeprefix("step-done:"))
        if step_event is None:
            return None
        parents.append(("agent_task_event", step_event.id))
    elif event_type in SUPPORTED_EVENT_TYPES:
        data = payload or {}
        if event_type in {"task_completed", "task_cancelled"}:
            task_id = data.get("task_id")
            task = reader.get_task(task_id) if isinstance(task_id, str) else None
            if task is None:
                return None
            parents.extend([("agent_task", task.id), ("goal_plan", task.id)])
            if task.source_turn_record_id:
                refs.add(("turn_record", task.source_turn_record_id))
        else:
            # Keep the domain source-time guard used by existing event dispatch.
            if event_type.startswith("knowledge_maintenance_"):
                operation_id = data.get("operation_id")
                origin = db.query(OutboxJob).filter(OutboxJob.job_type == "knowledge_maintenance",
                    OutboxJob.operation_id.in_([operation_id, f"knowledge_maintenance:{operation_id}"])).first() if isinstance(operation_id, str) else None
            else:
                request_id = data.get("request_id")
                origin = db.get(SelfDevRequest, request_id) if isinstance(request_id, str) else None
            if origin is None or blocked_sources_reason(db, SourceReferences(source_times=[origin.created_at])):
                return None
            if isinstance(origin, SelfDevRequest) and origin.source_event_id:
                refs.add(("event", origin.source_event_id))
            if isinstance(origin, OutboxJob):
                try:
                    collect_sources(origin.payload, refs)
                except ValueError:
                    return None
        try:
            collect_sources(data, refs)
        except ValueError:
            return None
    else:
        return None
    for kind, owner_id in parents:
        refs.update((r.source_type, r.source_id) for r in db.query(ContentProvenanceRef).filter_by(
            owner_type=kind, owner_id=owner_id).all())
    grouped: dict[str, list[str]] = {}
    for kind, token in refs:
        grouped.setdefault(kind, []).append(token)
    if not references_visible(db, grouped):
        return None
    keys = {"event": "source_event_ids", "turn_record": "source_turn_record_id", "memory_record": "memory_id"}
    return {keys[kind]: ids for kind, ids in grouped.items()}, parents


def _inbox_visible(db: Session, row: TurnEventInbox) -> bool:
    if not sources_visible(db, "turn_event_inbox", row.id):
        return False
    data = row.payload or {}
    task_id = data.get("task_id")
    if row.event_type == MAIN_AGENT_TASK_TERMINAL:
        task = TaskReadRepository(db).get_task(task_id) if isinstance(task_id, str) else None
        return task is not None and task.thread_id == row.thread_id and task.status == data.get("status")
    if task_id and (not isinstance(task_id, str) or TaskReadRepository(db).get_task(task_id) is None):
        return False
    return trigger_event_sources(db, str(data.get("trigger_event_id") or data.get("event_id") or ""),
        str(data.get("event_type") or ""), data.get("event_payload")) is not None


def enqueue_turn_event(
    db: Session,
    *,
    thread_id: str,
    event_type: str,
    source: str,
    dedupe_key: str,
    payload: dict[str, Any] | None = None,
    priority: int = 50,
    only_if_active: bool = True,
) -> TurnEventInbox | None:
    """持久化一条待注入事件；重复 dedupe_key 或没有活跃 Turn 时返回 None。

    调用方负责 commit。默认只接收活跃 Turn 的事件，任务终态也可在空闲时预先入箱。
    """
    if not thread_id or not dedupe_key:
        return None
    from aiive.forget.concurrency import acquire_forget_shared_lock
    acquire_forget_shared_lock(db)
    if only_if_active and active_turn_for_thread(db, thread_id) is None:
        return None
    existing = db.query(TurnEventInbox).filter(
        TurnEventInbox.dedupe_key == dedupe_key,
    ).one_or_none()
    if existing is not None:
        return None

    open_count = int(db.query(func.count(TurnEventInbox.id)).filter(
        TurnEventInbox.thread_id == thread_id,
        TurnEventInbox.status.in_(OPEN_STATUSES),
    ).scalar() or 0)
    if open_count >= MAX_PENDING_PER_THREAD:
        # 有界积压：优先淘汰最老的 pending，保留新事件；已注入/延后的事件不动。
        need = open_count - MAX_PENDING_PER_THREAD + 1
        oldest = db.query(TurnEventInbox).filter(
            TurnEventInbox.thread_id == thread_id,
            TurnEventInbox.status == STATUS_PENDING,
            TurnEventInbox.event_type != MAIN_AGENT_TASK_TERMINAL,
        ).order_by(
            TurnEventInbox.created_at.asc(), TurnEventInbox.id.asc(),
        ).limit(need).all()
        if len(oldest) < need:
            # 没有可淘汰的 pending（都在本 Turn 处理中）：拒绝新事件，避免无界增长。
            # 决策作业本身仍在 Outbox，会在前台 Turn 结束后按原路径兜底决策一次。
            logger.warning(
                "turn_event_inbox 已满且无可淘汰事件，拒绝新事件（由 Outbox 在 Turn 结束后兜底）: thread_id=%s key=%s limit=%d",
                thread_id, dedupe_key, MAX_PENDING_PER_THREAD,
            )
            return None
        for row in oldest:
            row.status = STATUS_EXPIRED
            row.decision = DECISION_EVICTED_OVERFLOW
            row.handled_at = _now()
            logger.warning(
                "turn_event_inbox 溢出淘汰: thread_id=%s key=%s reason=overflow limit=%d",
                thread_id, row.dedupe_key, MAX_PENDING_PER_THREAD,
            )

    row = TurnEventInbox(
        thread_id=thread_id,
        dedupe_key=dedupe_key[:160],
        event_type=event_type,
        source=source,
        priority=int(priority),
        payload=dict(payload or {}),
        status=STATUS_PENDING,
    )
    db.add(row)
    db.flush()
    data = dict(row.payload or {})
    proof = trigger_event_sources(db, str(data.get("trigger_event_id") or data.get("event_id") or ""),
        str(data.get("event_type") or ""), data.get("event_payload"))
    task_id = data.get("task_id")
    parents = list(proof[1]) if proof is not None else []
    if isinstance(task_id, str) and task_id:
        task = TaskReadRepository(db).get_task(task_id)
        if task is None:
            proof = None
        else:
            parents.extend([("agent_task", task_id), ("goal_plan", task_id)])
            if task.source_turn_record_id:
                persist_sources(db, "turn_event_inbox", row.id, {"source_turn_record_id": task.source_turn_record_id})
    # The wrapper ID can identify a Task/domain event; only persist verified chat IDs.
    for key in ("trigger_event_id", "event_id", "event_payload"):
        data.pop(key, None)
    persist_sources(db, "turn_event_inbox", row.id, data, proof[0] if proof else {})
    inherit_sources(db, "turn_event_inbox", row.id, parents)
    if proof is None:
        db.add(ContentProvenanceRef(owner_type="turn_event_inbox", owner_id=row.id,
            source_type="unresolved", source_id="trigger_source_unavailable"))
    db.flush()
    return row


def claim_pending_events(
    db: Session,
    *,
    thread_id: str,
    turn_record_id: str,
    turn_id: str,
    limit: int = MAX_INJECT_PER_CALL,
    task_terminal_only: bool = False,
    include_task_terminal: bool = True,
) -> list[TurnEventInbox]:
    """按优先级领取待注入事件；领取后状态置 injected，重复调用不会重复注入。"""
    from aiive.forget.concurrency import acquire_forget_shared_lock
    acquire_forget_shared_lock(db)
    query = db.query(TurnEventInbox).filter(
        TurnEventInbox.thread_id == thread_id,
        TurnEventInbox.status == STATUS_PENDING,
    )
    if task_terminal_only:
        query = query.filter(TurnEventInbox.event_type == MAIN_AGENT_TASK_TERMINAL)
        if not include_task_terminal:
            return []
    elif not include_task_terminal:
        query = query.filter(TurnEventInbox.event_type != MAIN_AGENT_TASK_TERMINAL)
    rows = query.order_by(
        TurnEventInbox.priority.desc(),
        TurnEventInbox.created_at.asc(),
        TurnEventInbox.id.asc(),
    ).limit(max(MAX_PENDING_PER_THREAD, limit)).with_for_update(skip_locked=True).all()
    if not rows:
        return []
    moment = _now()
    claimed: list[TurnEventInbox] = []
    for row in rows:
        if not _inbox_visible(db, row):
            row.status = STATUS_EXPIRED
            row.decision = "source_blocked"
            row.handled_at = moment
            continue
        if len(claimed) >= max(1, limit):
            continue
        row.status = STATUS_INJECTED
        row.turn_record_id = turn_record_id
        row.turn_id = turn_id
        row.injected_at = moment
        claimed.append(row)
    db.flush()
    return claimed


def expire_orphaned_task_events(db: Session, thread_id: str) -> None:
    """Release interrupted claims back to their Outbox jobs, including expired Turn leases."""
    active_claim = db.query(TurnRecord.id).filter(
        TurnRecord.id == TurnEventInbox.turn_record_id,
        TurnRecord.status == "running",
        TurnRecord.lease_expires_at > _now(),
    ).exists()
    db.query(TurnEventInbox).filter(
        TurnEventInbox.thread_id == thread_id,
        TurnEventInbox.event_type == MAIN_AGENT_TASK_TERMINAL,
        TurnEventInbox.status == STATUS_INJECTED,
        ~active_claim,
    ).update({TurnEventInbox.status: STATUS_EXPIRED, TurnEventInbox.decision: "turn_interrupted"},
             synchronize_session=False)


def stage_task_terminal_event(
    db: Session, thread_id: str, dedupe_key: str, payload: dict[str, Any],
) -> TurnEventInbox | None:
    """Called under the thread row lock, so concurrent Outbox claims share one Inbox row."""
    row = db.query(TurnEventInbox).filter_by(dedupe_key=dedupe_key).populate_existing().one_or_none()
    if row is None:
        return enqueue_turn_event(
            db, thread_id=thread_id, dedupe_key=dedupe_key, event_type=MAIN_AGENT_TASK_TERMINAL,
            source="task_runtime", payload={"task_id": payload["task_id"], "status": payload["status"]},
            only_if_active=False,
        )
    if row.status != STATUS_HANDLED and not _inbox_visible(db, row):
        row.status = STATUS_EXPIRED
        row.decision = "source_blocked"
    elif row.status == STATUS_EXPIRED and row.decision != "source_blocked":
        open_count = db.query(func.count(TurnEventInbox.id)).filter(
            TurnEventInbox.thread_id == thread_id, TurnEventInbox.status.in_(OPEN_STATUSES),
        ).scalar() or 0
        if open_count < MAX_PENDING_PER_THREAD:
            row.status = STATUS_PENDING
            row.turn_record_id = row.turn_id = row.decision = None
            row.injected_at = row.task_status_read_at = row.handled_at = None
    return row


def task_terminal_turn_live(db: Session, turn_record_id: str, execution_id: str) -> bool:
    return db.query(TurnRecord.id).filter(
        TurnRecord.id == turn_record_id, TurnRecord.execution_id == execution_id,
        TurnRecord.status == "running", TurnRecord.lease_expires_at > _now(),
    ).scalar() is not None


def claimed_task_terminal_events(db: Session, turn_record_id: str, *, for_update: bool = False) -> list[TurnEventInbox]:
    query = db.query(TurnEventInbox).filter_by(
        turn_record_id=turn_record_id, event_type=MAIN_AGENT_TASK_TERMINAL, status=STATUS_INJECTED,
    ).order_by(TurnEventInbox.created_at, TurnEventInbox.id)
    if for_update:
        query = query.with_for_update()
    return query.all()


def record_task_status_reads(db: Session, turn_record_id: str, statuses: dict[str, str]) -> None:
    """Store proof only for the task and terminal status returned to this Turn."""
    for row in claimed_task_terminal_events(db, turn_record_id, for_update=True):
        data = row.payload or {}
        task_id = str(data.get("task_id") or "")
        if task_id in statuses and statuses[task_id] == data.get("status") and _inbox_visible(db, row):
            row.task_status_read_at = _now()


def finish_task_terminal_events(
    db: Session, rows: list[TurnEventInbox], decisions: dict[str, str], *, has_reply: bool, wakeup: bool,
) -> bool:
    """Close validated observations atomically with the final reply. Return whether to show it."""
    accepted: set[str] = set()
    blocked: set[str] = set()
    for row in rows:
        decision = decisions.get(row.id)
        if not _inbox_visible(db, row):
            blocked.add(row.id)
        elif row.task_status_read_at and decision and decision != "defer" and (has_reply or decision == "no_action"):
            accepted.add(row.id)
    # A wakeup has no independent user question to answer. If any observation is
    # incomplete, discard the batch reply and let the respective Outbox jobs retry.
    show_reply = bool(rows) and len(accepted) == len(rows)
    if wakeup and not show_reply:
        accepted.clear()
    for row in rows:
        if row.id in accepted:
            row.status = STATUS_HANDLED
            row.decision = decisions[row.id]
            row.handled_at = _now()
        else:
            row.status = STATUS_EXPIRED
            row.decision = (
                "source_blocked" if row.id in blocked else
                "task_status_unread" if not row.task_status_read_at else
                "defer" if decisions.get(row.id) == "defer" else "task_review_incomplete"
            )
    return show_reply or not wakeup


def inbox_state(db: Session, dedupe_key: str) -> str:
    if not dedupe_key:
        return ""
    return str(db.query(TurnEventInbox.status).filter(
        TurnEventInbox.dedupe_key == dedupe_key[:160],
    ).scalar() or "")


def expire_pending_event(db: Session, dedupe_key: str) -> bool:
    """Turn 未处理的事件交回 Outbox：标记 expired，允许原决策作业继续。"""
    if not dedupe_key:
        return False
    row = db.query(TurnEventInbox).filter(
        TurnEventInbox.dedupe_key == dedupe_key[:160],
        TurnEventInbox.status.in_(OPEN_STATUSES),
    ).one_or_none()
    if row is None:
        return False
    row.status = STATUS_EXPIRED
    row.handled_at = _now()
    return True


def set_decisions(db: Session, event_ids: list[str], decisions: dict[str, str]) -> None:
    """记录本 Turn 对已注入事件的决策；状态保持 injected，由 finalize 收口。"""
    if not event_ids:
        return
    rows = db.query(TurnEventInbox).filter(
        TurnEventInbox.id.in_(event_ids),
        TurnEventInbox.status == STATUS_INJECTED,
        TurnEventInbox.event_type != MAIN_AGENT_TASK_TERMINAL,
    ).all()
    for row in rows:
        decision = str(decisions.get(row.id, "") or "")
        if decision:
            row.decision = decision[:32]


def mark_turn_handled(
    db: Session, turn_record_id: str, decisions: dict[str, str],
) -> int:
    """Turn 正常结束时收口：defer 保留给 Outbox 稍后决策，其余标记 handled。

    只处理仍为 injected 的行；notify_user 已写消息的行保持 handled 不被覆盖。
    """
    if not turn_record_id:
        return 0
    rows = db.query(TurnEventInbox).filter(
        TurnEventInbox.turn_record_id == turn_record_id,
        TurnEventInbox.status == STATUS_INJECTED,
        TurnEventInbox.event_type != MAIN_AGENT_TASK_TERMINAL,
    ).all()
    moment = _now()
    for row in rows:
        decision = str(decisions.get(row.id, "") or row.decision or "handled_in_turn")
        row.decision = decision[:32]
        if decision == "defer":
            row.status = STATUS_DEFERRED
            continue
        row.status = STATUS_HANDLED
        row.handled_at = moment
    return len(rows)


def claimed_rows_for_turn(db: Session, turn_record_id: str) -> list[TurnEventInbox]:
    """notify_user 的授权边界：只能针对本 Turn 已领取的事件发消息。"""
    if not turn_record_id:
        return []
    return db.query(TurnEventInbox).filter(
        TurnEventInbox.turn_record_id == turn_record_id,
        TurnEventInbox.status.in_((STATUS_INJECTED, STATUS_HANDLED)),
        or_(TurnEventInbox.event_type != MAIN_AGENT_TASK_TERMINAL,
            TurnEventInbox.status == STATUS_INJECTED),
    ).order_by(TurnEventInbox.created_at.asc()).all()


def can_notify_for_event(db: Session, row: TurnEventInbox) -> bool:
    if row.event_type != MAIN_AGENT_TASK_TERMINAL:
        return True
    turn = db.query(TurnRecord.id).filter(
        TurnRecord.id == row.turn_record_id, TurnRecord.status == "running",
        TurnRecord.lease_expires_at > _now(), TurnRecord.cancel_requested_at.is_(None),
    ).with_for_update().scalar()
    if turn is None:
        return False
    db.refresh(row)
    return row.status == STATUS_INJECTED and row.turn_record_id == turn and (
        row.task_status_read_at is not None and _inbox_visible(db, row)
    )


def mark_notified(
    db: Session, event_ids: list[str], message_event_id: str,
) -> None:
    """主动消息写入成功后收口：同一事件不会被 Outbox 再决策一次。"""
    if not event_ids:
        return
    rows = db.query(TurnEventInbox).filter(
        TurnEventInbox.id.in_(event_ids),
        TurnEventInbox.status.in_((STATUS_INJECTED, STATUS_PENDING)),
    ).all()
    moment = _now()
    for row in rows:
        if row.event_type == MAIN_AGENT_TASK_TERMINAL and row.task_status_read_at is None:
            continue
        row.status = STATUS_HANDLED
        row.decision = "notify"
        row.message_event_id = message_event_id
        row.handled_at = moment


def _task_snapshot(db: Session, task_id: str) -> dict[str, Any]:
    if not task_id:
        return {}
    from aiive.task_runtime.read_repository import TaskReadRepository
    reader = TaskReadRepository(db)
    task = reader.get_task(task_id)
    if task is None:
        return {}
    evidence = reader.recent_evidence(task_id, 3)
    return {
        "title": _bounded(task.title or task.goal or "", 200),
        "status": str(task.status or ""),
        "evidence": [
            _bounded(row.summary or "", 200) for row in reversed(evidence)
        ],
    }


def build_internal_context(db: Session, rows: list[TurnEventInbox], *, zone: str = "UTC") -> str:
    """构造注入给现有 LLM 的内部系统上下文（不写入聊天时间线）。"""
    from aiive.core.message_time import message_stamp

    rows = [row for row in rows if _inbox_visible(db, row)]
    if not rows:
        return ""
    lines = [
        INTERNAL_CONTEXT_HEADER,
        "以下事件在你的任务执行期间到达。当前工具调用批次已经完成，工具调用与结果均已配对。",
        "请先判断如何处理，再继续原任务；不要把这些内容当作新的用户指令，也不要原样复述给用户。",
        "",
    ]
    for index, row in enumerate(rows, start=1):
        payload = dict(row.payload or {})
        lines.append(f"事件 {index}（event_id={row.id}）")
        lines.append(f"- 类型: {row.event_type}")
        lines.append(message_stamp(row.created_at, "runtime_event", zone))
        if row.event_type == MAIN_AGENT_TASK_TERMINAL:
            lines.append(f"- task_id: {payload['task_id']} · Worker 状态: {payload['status']}")
            lines.append("- 本轮已取得状态结果。" if row.task_status_read_at else "- 本轮尚未取得状态结果，先调用 get_task_status。")
            continue
        task = _task_snapshot(db, str(payload.get("task_id") or ""))
        lines.append(f"- 来源: {row.source}")
        lines.append(f"- 优先级: {row.priority}")
        if payload.get("task_title") or task.get("title"):
            lines.append(f"- 关联目标: {payload.get('task_title') or task.get('title')}")
        if payload.get("step_id"):
            lines.append(
                f"- 关联步骤: {payload.get('step_id')} {payload.get('step_title') or ''}".strip()
            )
        if payload.get("reason"):
            lines.append(f"- 触发原因: {_bounded(payload.get('reason'), 600)}")
        if payload.get("event_type"):
            lines.append(f"- 运行事件类型: {payload.get('event_type')}")
        event_payload = payload.get("event_payload")
        if isinstance(event_payload, dict) and event_payload:
            lines.append(
                "- 事件元数据: "
                + _bounded(json.dumps(event_payload, ensure_ascii=False, default=str), 800)
            )
        if task.get("status"):
            lines.append(f"- 当前目标状态: {task['status']}")
        if task.get("evidence"):
            lines.append("- 相关证据: " + "；".join(task["evidence"]))
        lines.append("")

    if any(row.event_type == MAIN_AGENT_TASK_TERMINAL for row in rows):
        lines.append(get_prompt_registry().render("agent.task_terminal_review").content)
    if any(row.event_type != MAIN_AGENT_TASK_TERMINAL for row in rows):
        lines.extend([
            "处理要求（必须明确选择一种，并在回复第一行给出标记，标记不会展示给用户）：",
            "- 立即处理：直接调用所需工具；如同时需要提醒用户，调用 notify_user（同一批工具调用）。",
            "- 延后：输出 [[EVENT_DECISION: defer]]，系统会在本 Turn 结束后交给原目标决策流程。",
            "- 继续当前任务：输出 [[EVENT_DECISION: continue]]。",
            "- 无需行动：输出 [[EVENT_DECISION: no_action]]。",
            "notify_user 只用于向用户发送一条独立提醒，消息会写入聊天时间线；",
            "不要把提醒内容混进原任务的最终答复，发完后继续完成原任务。",
            "涉及取消、修改计划或审批时，只能使用现有工具与安全内核，不得绕过。",
        ])
    return "\n".join(lines)


def remove_decision_markers(text: str) -> str:
    """剥离全部决策标记，返回与流式过滤器一致的清洗结果。

    只有标记出现在首个非空白字符之前时才丢弃前导空白——该规则可由流式
    过滤器增量复现，因此"流式累计文本"与"最终持久化文本"逐字相同。
    """
    raw = str(text or "")
    match = DECISION_MARKER_RE.search(raw)
    if match is None:
        return raw
    cleaned = DECISION_MARKER_RE.sub("", raw)
    if not raw[: match.start()].strip():
        cleaned = cleaned.lstrip()
    return cleaned


def parse_decision_marker(text: str) -> tuple[str, str | None]:
    """从模型输出中提取显式决策标记；返回 (清洗后文本, decision)。"""
    raw = str(text or "")
    match = DECISION_MARKER_RE.search(raw)
    decision = match.group(1).lower() if match else None
    return remove_decision_markers(raw), decision


def decision_from_response(content: str, tool_names: list[str]) -> tuple[str, str]:
    """行为优先：notify_user > 其他工具 > 显式标记 > 默认继续。"""
    cleaned, marker = parse_decision_marker(content)
    if "notify_user" in tool_names:
        return cleaned, "notify"
    if tool_names:
        return cleaned, marker or "act_now"
    return cleaned, marker or "continue"
