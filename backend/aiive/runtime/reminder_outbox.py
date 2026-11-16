"""提醒投递 Outbox 的唯一实现入口。

手动单条入队（TaskManager.enqueue_reminder_now）与到期批量扫描
（worker.task_worker.enqueue_due_tasks）只在「如何选中 Task」上不同：
前者按 ID 锁定单行，后者用 SKIP LOCKED 批量认领。两者必须共用同一
operation_id、payload、幂等判断与 Task 状态推进，避免出现两套投递语义。

终态收敛：同一 operation_id 的 OutboxJob 一旦进入终态，Task 必须跟随该
终态收敛，否则 pending Task 会永远满足扫描条件并无限重扫。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from aiive.db.models import OutboxJob, Task

REMINDER_DELIVERY_JOB_TYPE = "reminder_delivery"
REMINDER_SCHEMA_VERSION = 1
REMINDER_MAX_RETRIES = 3

_ACTIVE_JOB_STATUSES = frozenset({"pending", "running"})
_TERMINAL_TASK_STATUS = {"completed": "completed", "cancelled": "cancelled"}


def reminder_operation_id(task_id: str) -> str:
    """提醒投递的幂等身份：每个 Task 只允许一条投递记录。"""
    return f"{REMINDER_DELIVERY_JOB_TYPE}:{task_id}"


def due_reminder_tasks(
    db: Session, now: datetime, *, skip_locked: bool = False,
) -> list[Task]:
    """到期提醒的唯一查询定义。

    批量扫描用 skip_locked=True 原子认领；只读兼容入口（TaskManager.get_due）
    用默认行锁语义，二者共享同一筛选与排序条件。
    """
    query = (
        db.query(Task)
        .filter(
            Task.status == "pending",
            Task.task_type == "reminder",
            Task.next_check_at <= now,
        )
        .order_by(Task.next_check_at.asc(), Task.id.asc())
    )
    if skip_locked:
        query = query.with_for_update(skip_locked=True)
    return query.all()


def build_reminder_delivery_job(task: Task) -> OutboxJob:
    """按 Task 当前事实构造唯一投递 Job（不落库，由调用方 add）。"""
    operation_id = reminder_operation_id(task.id)
    return OutboxJob(
        operation_id=operation_id,
        job_type=REMINDER_DELIVERY_JOB_TYPE,
        status="pending",
        payload={
            "schema_version": REMINDER_SCHEMA_VERSION,
            "operation_id": operation_id,
            "task_id": task.id,
            "task_type": task.task_type,
            "thread_id": task.thread_id or "system",
            "title": task.title,
            "content": task.title,
            "scheduled_at": task.next_check_at.isoformat() if task.next_check_at else None,
        },
        trace_id=task.id,
        max_retries=REMINDER_MAX_RETRIES,
    )


def claim_reminder_delivery(db: Session, task: Task, now: datetime) -> str:
    """认领一个提醒投递，返回 enqueued / already_dispatching / converged。

    调用方必须已锁定 task（手动入口 FOR UPDATE，批量入口 SKIP LOCKED）。
    函数只 flush，不提交事务，保证 Task 状态与 OutboxJob 创建原子。
    """
    existing = (
        db.query(OutboxJob)
        .filter(OutboxJob.operation_id == reminder_operation_id(task.id))
        .with_for_update()
        .first()
    )
    if existing is not None:
        if existing.status in _ACTIVE_JOB_STATUSES:
            task.status = "dispatching"
            task.last_checked_at = now
            return "already_dispatching"
        task.status = _TERMINAL_TASK_STATUS.get(existing.status, "failed")
        task.last_checked_at = now
        return "converged"

    db.add(build_reminder_delivery_job(task))
    task.next_check_at = now
    task.status = "dispatching"
    task.last_checked_at = now
    db.flush()
    return "enqueued"
