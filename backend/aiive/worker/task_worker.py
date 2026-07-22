"""提醒任务扫描器：将到期任务可靠地写入 Outbox。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.base import SessionLocal
from aiive.db.models import OutboxJob, Task
from aiive.runtime.task_manager import TaskManager
from aiive.runtime.thread_bootstrap import ThreadBootstrapService

logger = logging.getLogger(__name__)


def enqueue_due_tasks(db: Session, now: datetime | None = None) -> list[dict[str, Any]]:
    """原子认领到期任务并创建唯一的提醒投递 OutboxJob。

    Scheduler 与手动 run_once 共用此入口。该函数只 flush，不提交事务；
    调用方负责提交，使 Task 状态和 OutboxJob 创建保持原子。
    """
    now = now or datetime.now(timezone.utc)
    due = (
        db.query(Task)
        .filter(
            Task.status == "pending",
            Task.task_type == "reminder",
            Task.next_check_at <= now,
        )
        .order_by(Task.next_check_at.asc(), Task.id.asc())
        .with_for_update(skip_locked=True)
        .all()
    )
    results: list[dict[str, Any]] = []

    for task in due:
        target_thread_id = task.thread_id or "system"
        operation_id = f"reminder_delivery:{task.id}"
        existing = (
            db.query(OutboxJob)
            .filter(OutboxJob.operation_id == operation_id)
            .first()
        )
        if existing is None:
            db.add(OutboxJob(
                operation_id=operation_id,
                job_type="reminder_delivery",
                status="pending",
                payload={
                    "schema_version": 1,
                    "operation_id": operation_id,
                    "task_id": task.id,
                    "task_type": task.task_type,
                    "thread_id": target_thread_id,
                    "title": task.title,
                    "content": task.title,
                    "scheduled_at": task.next_check_at.isoformat() if task.next_check_at else None,
                },
                trace_id=task.id,
                max_retries=3,
            ))
        elif existing.status not in {"pending", "running"}:
            logger.error(
                "到期提醒存在终态 Outbox，拒绝将 Task 置为 dispatching: task_id=%s job_status=%s",
                task.id,
                existing.status,
            )
            continue

        task.status = "dispatching"
        task.last_checked_at = now
        results.append({
            "task_id": task.id,
            "title": task.title,
            "status": "dispatching",
            "operation_id": operation_id,
        })

    db.flush()
    return results


class TaskWorker:
    """兼容旧调用方的任务扫描入口，不再直接执行提醒。"""

    def __init__(self, db: Session):
        self._db = db

    def poll_and_notify(self) -> list[dict[str, Any]]:
        """扫描到期任务并入队，投递由 reminder_delivery Handler 完成。"""
        results = enqueue_due_tasks(self._db)
        self._db.commit()
        return results


def run_once() -> list[dict[str, Any]]:
    """手动执行一次与生产一致的到期任务扫描。"""
    db = SessionLocal()
    try:
        results = enqueue_due_tasks(db)
        db.commit()
        for result in results:
            print(f"[TASK] Enqueued: {result['title'][:50]}")
        return results
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
