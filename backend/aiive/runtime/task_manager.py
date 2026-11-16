"""
运行时层 - 任务管理器。

负责提醒（reminder）的创建、查询、取消与入队。提醒投递的 operation_id、
payload、幂等与终态收敛统一由 runtime/reminder_outbox 实现，本模块只保留
Task 侧的锁定与调用编排。

condition_watch / routine 已不再由生产入口创建：周期与条件性需求由
Persistent Task Runtime 的 Watcher 与 Goal Trigger 承担。
"""
from datetime import datetime, timedelta, timezone
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from aiive.db.models import Event, OutboxJob, Task
from aiive.runtime.reminder_outbox import (
    claim_reminder_delivery,
    due_reminder_tasks,
    reminder_operation_id,
)


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
        """只读列出当前到期的提醒（兼容入口）。

        权威定义在 runtime.reminder_outbox.due_reminder_tasks；本方法不包含
        独立筛选逻辑，也不再返回无法自动执行的旧任务类型。
        """
        return due_reminder_tasks(self._db, now or datetime.now(timezone.utc))

    def next_due_at(self) -> datetime | None:
        """获取最早可自动投递的提醒到期时间，用于精确调度。

        只有 reminder 会被扫描器自动入队；condition_watch / routine 等
        无法自动执行的任务不得参与调度，否则过期时间会让轮询空转到 1s。
        """
        task = (
            self._db.query(Task)
            .filter(
                Task.status == "pending",
                Task.task_type == "reminder",
                Task.next_check_at.is_not(None),
            )
            .order_by(Task.next_check_at.asc())
            .first()
        )
        return task.next_check_at if task else None

    def enqueue_reminder_now(self, task_id: str) -> dict[str, Any]:
        """按任务 ID 原子入队提醒，不扫描其他任务。

        与批量扫描共用 runtime/reminder_outbox 的唯一投递构造、operation_id、
        幂等判断与终态收敛；本入口只负责按 ID 锁定单行。
        """
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

        outcome = claim_reminder_delivery(self._db, task, datetime.now(timezone.utc))
        if outcome == "converged":
            return {
                "ok": False,
                "status": "stale_job",
                "error": "提醒任务已有终态投递记录，已按投递终态收敛任务状态",
                "task_id": task.id,
                "task_status": task.status,
            }
        return {
            "ok": True,
            "status": outcome,
            "task_id": task.id,
            "title": task.title,
            "operation_id": reminder_operation_id(task.id),
        }

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
                OutboxJob.operation_id == reminder_operation_id(task.id),
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
            "events_cancelled": len(related),
        }

    def check_now(self, task_id: str) -> dict[str, Any]:
        """立即检查并处理指定任务。

        根据任务类型执行不同逻辑：
        - reminder：仅返回需要入队的动作，不提前完成任务
        - condition_watch / routine：遗留兼容分支。生产入口已不再创建这两类
          Task（POST /api/tasks 只接受 reminder），其调度职责由 Persistent
          Task Runtime 的 Watcher 与 Goal Trigger 承担；这里只保留对历史数据
          与既有测试语义的最小支持，不参与到期扫描。

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
