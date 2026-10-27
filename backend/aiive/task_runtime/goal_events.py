"""真实运行事件 → 目标触发匹配的唯一发布入口。

职责边界（复用优先，不新建事件总线）：
- 事件不落独立事件表：它作为 OutboxJob（`goal_event_dispatch`）的载荷随事务落库，
  投递、重试、进程重启恢复全部复用既有 Outbox（operation_id 唯一 + claim token）；
- 只携带最小标识与元数据，详细上下文由消费者按需读取（目标计划 / Evidence / 对话）；
- 事件 id 由生产者给出且稳定，重复发布由 `goal_event:<source>:<event_id>` 唯一约束去重；
- 没有活跃目标订阅时直接丢弃，避免给无目标用户制造 Outbox 噪声。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import AgentTask, OutboxJob
from aiive.task_runtime.goal_plan import GOAL_TASK_TYPE
from aiive.task_runtime.repository import utcnow
from aiive.task_runtime.schemas import TaskStatus

logger = logging.getLogger(__name__)

DISPATCH_JOB_TYPE = "goal_event_dispatch"

# 已接入的稳定事件类型。只登记真实存在生产者的事件，不为凑齐类型虚构事件。
TASK_COMPLETED = "task_completed"
TASK_CANCELLED = "task_cancelled"
KNOWLEDGE_MAINTENANCE_COMPLETED = "knowledge_maintenance_completed"
KNOWLEDGE_MAINTENANCE_FAILED = "knowledge_maintenance_failed"
SELFDEV_REPAIR_COMPLETED = "selfdev_repair_completed"
SELFDEV_REPAIR_FAILED = "selfdev_repair_failed"

SUPPORTED_EVENT_TYPES: frozenset[str] = frozenset({
    TASK_COMPLETED,
    TASK_CANCELLED,
    KNOWLEDGE_MAINTENANCE_COMPLETED,
    KNOWLEDGE_MAINTENANCE_FAILED,
    SELFDEV_REPAIR_COMPLETED,
    SELFDEV_REPAIR_FAILED,
})

# 可被事件唤醒的目标任务状态：运行中的任务让出本轮，等结算后再匹配。
ACTIVE_GOAL_STATES: frozenset[str] = frozenset({
    TaskStatus.QUEUED.value,
    TaskStatus.BLOCKED_USER.value,
    TaskStatus.BLOCKED_NODE.value,
    TaskStatus.BLOCKED_APPROVAL.value,
})

# 单条事件的元数据上限：超出时只保留标识，避免把整份报告塞进 Outbox 载荷。
MAX_EVENT_PAYLOAD_CHARS = 2000


@dataclass(frozen=True)
class GoalEvent:
    """投递给目标触发匹配的最小事件视图。"""

    event_id: str
    event_type: str
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=utcnow)
    thread_id: str | None = None


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _trace_id(operation_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, operation_id))


def _bounded_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """裁剪事件元数据：过大时只保留可定位的标识字段。"""
    data = dict(payload or {})
    try:
        encoded = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"unserializable": True}
    if len(encoded) <= MAX_EVENT_PAYLOAD_CHARS:
        return data
    kept = {
        key: value
        for key, value in data.items()
        if isinstance(value, (str, int, float, bool)) and key in {
            "task_id", "status", "task_type", "run_id", "operation_id",
            "request_id", "goal", "title", "failure_reason", "reason",
        }
    }
    kept["truncated"] = True
    return kept


def has_active_goal_subscribers(db: Session, *, thread_id: str | None = None) -> bool:
    """是否存在可能订阅该事件的目标；无目标时不制造 Outbox 噪声。"""
    query = db.query(AgentTask.id).filter(
        AgentTask.task_type == GOAL_TASK_TYPE,
        AgentTask.status.in_(tuple(ACTIVE_GOAL_STATES)),
    )
    if thread_id:
        query = query.filter(AgentTask.thread_id == thread_id)
    return db.query(query.exists()).scalar() is True


def publish_goal_event(
    db: Session,
    *,
    event_type: str,
    event_id: str,
    source: str,
    payload: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
    thread_id: str | None = None,
    require_subscribers: bool = True,
) -> OutboxJob | None:
    """把一条真实运行事件可靠地投递给目标触发匹配。

    返回新建的 OutboxJob；重复发布（同一 source + event_id）返回 None。
    调用方负责 commit，与业务写入保持同一事务。
    """
    if event_type not in SUPPORTED_EVENT_TYPES:
        raise ValueError(f"unsupported goal event type: {event_type}")
    if not event_id:
        raise ValueError("goal event requires a stable event_id")
    if require_subscribers and not has_active_goal_subscribers(db, thread_id=thread_id):
        return None
    operation_id = f"goal_event:{source}:{event_id}"
    if len(operation_id) > 128:
        digest = hashlib.sha1(operation_id.encode("utf-8")).hexdigest()[:16]
        operation_id = f"goal_event:{source}:{digest}"
    existing = db.query(OutboxJob).filter(OutboxJob.operation_id == operation_id).one_or_none()
    if existing is not None:
        return None
    moment = occurred_at or utcnow()
    job = OutboxJob(
        operation_id=operation_id,
        job_type=DISPATCH_JOB_TYPE,
        status="pending",
        payload={
            "event_id": event_id,
            "event_type": event_type,
            "source": source,
            "thread_id": thread_id,
            "occurred_at": _iso(moment),
            "event_payload": _bounded_payload(payload),
        },
        trace_id=_trace_id(operation_id),
        schema_version=1,
        max_retries=3,
    )
    db.add(job)
    db.flush()
    return job


def event_from_job_payload(payload: dict[str, Any]) -> GoalEvent | None:
    """把 Outbox 载荷还原成事件视图；字段不全时返回 None（不可重试）。"""
    event_type = str(payload.get("event_type") or "")
    event_id = str(payload.get("event_id") or "")
    source = str(payload.get("source") or "")
    if not event_type or not event_id or not source:
        return None
    if event_type not in SUPPORTED_EVENT_TYPES:
        return None
    raw_moment = payload.get("occurred_at")
    moment = utcnow()
    if isinstance(raw_moment, str) and raw_moment.strip():
        try:
            parsed = datetime.fromisoformat(raw_moment.strip().replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raw_payload = payload.get("event_payload")
    thread_id = payload.get("thread_id")
    return GoalEvent(
        event_id=event_id,
        event_type=event_type,
        source=source,
        payload=dict(raw_payload) if isinstance(raw_payload, dict) else {},
        occurred_at=moment,
        thread_id=str(thread_id) if thread_id else None,
    )


def publish_task_terminal_event(db: Session, task: AgentTask, status: str) -> OutboxJob | None:
    """任务到达终态时发布事件；目标自身任务不发布，避免自激。"""
    if task.task_type == GOAL_TASK_TYPE:
        return None
    event_type = TASK_CANCELLED if status == TaskStatus.CANCELLED.value else TASK_COMPLETED
    return publish_goal_event(
        db,
        event_type=event_type,
        event_id=f"{task.id}:{status}",
        source="task",
        thread_id=task.thread_id,
        payload={
            "task_id": task.id,
            "status": status,
            "task_type": task.task_type,
            "title": task.title,
        },
    )
