"""提醒任务扫描器：将到期任务可靠地写入 Outbox。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from aiive.runtime.reminder_outbox import (
    claim_reminder_delivery,
    due_reminder_tasks,
    reminder_operation_id,
)

logger = logging.getLogger(__name__)


def enqueue_due_tasks(db: Session, now: datetime | None = None) -> list[dict[str, Any]]:
    """原子认领到期提醒并创建唯一的投递 OutboxJob。

    Scheduler 使用此入口。投递构造、operation_id、幂等判断与终态收敛都来自
    runtime.reminder_outbox；本函数只负责批量选中到期 Task（SKIP LOCKED）。
    该函数只 flush，不提交事务；调用方负责提交，使 Task 状态和 OutboxJob
    创建保持原子。
    """
    now = now or datetime.now(timezone.utc)
    due = due_reminder_tasks(db, now, skip_locked=True)
    results: list[dict[str, Any]] = []

    for task in due:
        outcome = claim_reminder_delivery(db, task, now)
        if outcome == "converged":
            logger.error(
                "到期提醒存在终态 Outbox，按 Job 终态收敛 Task: task_id=%s task_status=%s",
                task.id,
                task.status,
            )
            continue
        results.append({
            "task_id": task.id,
            "title": task.title,
            "status": "dispatching",
            "operation_id": reminder_operation_id(task.id),
        })

    db.flush()
    return results
