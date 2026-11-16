"""
运行时层 - 任务管理器。

负责提醒（reminder）的创建、查询、取消与入队。提醒投递的 operation_id、
payload、幂等与终态收敛统一由 runtime/reminder_outbox 实现，本模块只保留
Task 侧的锁定与调用编排。

condition_watch / routine 已退役：生产入口不再创建，也不存在执行分支；
历史行保留可查询、可取消，但不会被解释为可执行任务。周期与条件性需求由
Persistent Task Runtime 的 Watcher 与 Goal Trigger 承担。
"""
from datetime import datetime, timezone
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from aiive.db.models import Event, OutboxJob, Task
from aiive.runtime.reminder_outbox import (
    claim_reminder_delivery,
    reminder_operation_id,
)

REMINDER_TASK_TYPE = "reminder"


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
            task_type: 任务类型；Task 只承载 reminder
            title: 任务标题
            description: 任务描述
            condition: 仅供历史数据兼容，新建提醒不使用
            next_check_at: 下次检查时间

        Returns:
            新创建的 Task 对象

        Raises:
            ValueError: task_type 不是 reminder（历史旧类型不再允许新建）
        """
        if task_type != REMINDER_TASK_TYPE:
            raise ValueError("unsupported_task_type")
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

    def next_due_at(self) -> datetime | None:
        """获取最早可自动投递的提醒到期时间，用于精确调度。

        只有 reminder 会被扫描器自动入队；历史旧类型与空时间不参与调度，
        否则过期时间会让轮询空转到 1s。
        """
        task = (
            self._db.query(Task)
            .filter(
                Task.status == "pending",
                Task.task_type == REMINDER_TASK_TYPE,
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
        if task.task_type != REMINDER_TASK_TYPE:
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
        """只取消 pending/dispatching；终态重试无副作用，事务由调用方提交。

        ok 表示请求成功，task_cancelled 表示最终状态，changed 表示本次迁移。
        events_cancelled 只统计本次同步处理的 reminder_created 事件。
        """
        task = (
            self._db.query(Task)
            .filter(Task.id == task_id)
            .with_for_update()
            .first()
        )
        result = {
            "ok": False,
            "task_id": task_id,
            "task_cancelled": False,
            "changed": False,
            "status": task.status if task is not None else "not_found",
            "events_cancelled": 0,
        }
        if task is None:
            return {**result, "error": "任务不存在"}
        if task.status == "cancelled":
            return {**result, "ok": True, "task_cancelled": True}
        if task.status not in {"pending", "dispatching"}:
            return {**result, "error": "任务当前状态不允许取消"}

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
            "task_cancelled": True,
            "changed": True,
            "events_cancelled": len(related),
        }

    def check_now(self, task_id: str) -> dict[str, Any]:
        """立即处理指定任务。

        Task 只承载可自动投递的提醒（reminder）：
        - reminder：按 ID 原子入队投递，不提前完成任务；
        - condition_watch / routine 等历史类型：明确拒绝，不改状态、不伪造通知。

        Args:
            task_id: 任务 ID

        Returns:
            包含 ok、status、title 等字段的结果字典
        """
        task = self._db.get(Task, task_id)
        if not task:
            return {"ok": False, "status": "not_found", "error": "Task not found"}
        if task.task_type != REMINDER_TASK_TYPE:
            return {
                "ok": False,
                "status": "unsupported_task_type",
                "task_id": task.id,
                "task_type": task.task_type,
                "error": "历史任务类型不再支持立即执行",
            }
        return self.enqueue_reminder_now(task_id)
