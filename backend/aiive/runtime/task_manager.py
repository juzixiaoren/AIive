"""
运行时层 - 任务管理器。

负责 Agent 定时任务的全生命周期管理：创建、查询、到期检查。
支持提醒（reminder）、条件监视（condition_watch）和例行任务（routine）三种类型。
"""
from datetime import datetime, timedelta, timezone
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from aiive.db.models import Event, OutboxJob, Task


class TaskManager:
    """任务管理器：封装 Task 模型的创建、查询和到期检查逻辑。

    Attributes:
        _db: SQLAlchemy 数据库会话
    """
    def __init__(self, db: Session):
        self._db: Session = db

    def create(
        self,
        task_type: str,
        title: str,
        description: str = "",
        condition: str | None = None,
        next_check_at: datetime | None = None,
    ) -> Task:
        """创建一个新任务。

        Args:
            task_type: 任务类型（reminder / condition_watch / routine）
            title: 任务标题
            description: 任务描述
            condition: 触发条件（提醒时间字符串 / 条件表达式 / 间隔分钟数）
            next_check_at: 下次检查时间

        Returns:
            新创建的 Task 对象
        """
        task = Task(
            task_type=task_type,
            title=title,
            description=description,
            condition=condition,
            next_check_at=next_check_at,
        )
        self._db.add(task)
        return task

    def list_all(self, status: str | None = None, thread_id: str | None = None) -> Sequence[Task]:
        """列出所有任务，支持按状态和线程过滤。

        Args:
            status: 任务状态过滤（如 pending、completed）
            thread_id: 线程 ID 过滤

        Returns:
            最多 50 条 Task 对象列表
        """
        q = self._db.query(Task).order_by(Task.created_at.desc())
        if status:
            q = q.filter(Task.status == status)
        if thread_id:
            q = q.filter(Task.thread_id == thread_id)
        return q.limit(50).all()

    def get_due(self, now: datetime | None = None) -> list[Task]:
        """获取所有已到期的待处理任务。

        Args:
            now: 当前时间（可选，默认 UTC 当前时间）

        Returns:
            已到期且状态为 pending 的 Task 列表
        """
        now = now or datetime.now(timezone.utc)
        return (
            self._db.query(Task)
            .filter(
                Task.status == "pending",
                Task.next_check_at <= now,
            )
            .all()
        )

    def next_due_at(self) -> datetime | None:
        """获取最早待处理任务的到期时间，用于精确调度。

        Returns:
            最早到期时间，无待处理任务时返回 None
        """
        task = (
            self._db.query(Task)
            .filter(Task.status == "pending")
            .order_by(Task.next_check_at.asc())
            .first()
        )
        return task.next_check_at if task else None

    def enqueue_reminder_now(self, task_id: str) -> dict[str, Any]:
        """按任务 ID 原子入队提醒，不扫描其他任务。"""
        task = (
            self._db.query(Task)
            .filter(Task.id == task_id)
            .with_for_update()
            .first()
        )
        if task is None:
            return {"ok": False, "status": "not_found", "error": "Task not found"}
        if task.task_type != "reminder":
            return {"ok": False, "status": "invalid_type", "error": "Task is not a reminder"}
        if task.status == "dispatching":
            return {"ok": True, "status": "already_dispatching", "task_id": task.id, "title": task.title}
        if task.status == "completed":
            return {"ok": True, "status": "already_completed", "task_id": task.id, "title": task.title}
        if task.status in {"cancelled", "failed"}:
            return {"ok": False, "status": task.status, "error": f"提醒任务已处于 {task.status} 状态"}

        now = datetime.now(timezone.utc)
        operation_id = f"reminder_delivery:{task.id}"
        existing = (
            self._db.query(OutboxJob)
            .filter(OutboxJob.operation_id == operation_id)
            .with_for_update()
            .first()
        )
        if existing is not None:
            if existing.status in {"pending", "running"}:
                task.status = "dispatching"
                task.last_checked_at = now
                return {"ok": True, "status": "already_dispatching", "task_id": task.id, "title": task.title}
            return {"ok": False, "status": "stale_job", "error": "提醒任务已有终态投递记录，不能复活原任务"}

        task.next_check_at = now
        task.status = "dispatching"
        task.last_checked_at = now
        self._db.add(OutboxJob(
            operation_id=operation_id,
            job_type="reminder_delivery",
            status="pending",
            payload={
                "schema_version": 1,
                "operation_id": operation_id,
                "task_id": task.id,
                "task_type": task.task_type,
                "thread_id": task.thread_id or "system",
                "title": task.title,
                "content": task.title,
                "scheduled_at": task.next_check_at.isoformat() if task.next_check_at else None,
            },
            trace_id=task.id,
            max_retries=3,
        ))
        self._db.flush()
        return {"ok": True, "status": "enqueued", "task_id": task.id, "title": task.title, "operation_id": operation_id}

    def cancel(self, task_id: str) -> dict[str, Any]:
        """取消尚未完成的任务，并终止未领取的提醒投递。"""
        task = (
            self._db.query(Task)
            .filter(Task.id == task_id)
            .with_for_update()
            .first()
        )
        if task is None:
            return {"ok": False, "status": "not_found", "error": "任务不存在"}

        cancelled = task.status in {"pending", "dispatching"}
        if cancelled:
            task.status = "cancelled"
            self._db.query(OutboxJob).filter(
                OutboxJob.operation_id == f"reminder_delivery:{task.id}",
                OutboxJob.status == "pending",
            ).update({
                OutboxJob.status: "cancelled",
                OutboxJob.terminal_reason: "task_cancelled",
                OutboxJob.terminal_at: func.now(),
            }, synchronize_session=False)

        related = (
            self._db.query(Event)
            .filter(
                Event.event_type == "reminder_created",
                Event.payload["task_id"].as_string() == task.id,
            )
            .all()
        )
        for event in related:
            payload = dict(event.payload or {})
            payload["status"] = "cancelled"
            payload["dismissed"] = True
            event.payload = payload

        return {
            "ok": True,
            "task_id": task.id,
            "status": task.status,
            "task_cancelled": cancelled,
        }

    def check_now(self, task_id: str) -> dict[str, Any]:
        """立即检查并处理指定任务。

        根据任务类型执行不同逻辑：
        - reminder：仅返回需要入队的动作，不提前完成任务
        - condition_watch：检查条件，满足则完成，否则保持待处理
        - routine：根据间隔重新调度下一次检查

        Args:
            task_id: 任务 ID

        Returns:
            包含 ok、action、title 等字段的结果字典
        """
        task = self._db.get(Task, task_id)
        if not task:
            return {"ok": False, "error": "Task not found"}
        if task.task_type == "reminder":
            return self.enqueue_reminder_now(task_id)

        now = datetime.now(timezone.utc)
        task.last_checked_at = now

        if task.task_type == "condition_watch":
            # 简单条件判断：条件为 "true" 时触发
            condition_met = task.condition and task.condition.lower() == "true"
            task.status = "completed" if condition_met else "pending"
            if condition_met:
                return {"ok": True, "action": "notify", "title": task.title}
            return {"ok": True, "action": "skip", "reason": "condition not met"}

        elif task.task_type == "routine":
            if task.condition:
                try:
                    interval_minutes = int(task.condition)
                    task.next_check_at = now + timedelta(minutes=interval_minutes)
                    task.status = "pending"
                except ValueError:
                    task.status = "completed"
            else:
                task.status = "completed"
            return {"ok": True, "action": "notify", "title": task.title}

        return {"ok": True, "action": "checked"}
