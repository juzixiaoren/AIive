"""
运行时层 - 任务管理器。

负责 Agent 定时任务的全生命周期管理：创建、查询、到期检查。
支持提醒（reminder）、条件监视（condition_watch）和例行任务（routine）三种类型。
"""
from datetime import datetime, timedelta, timezone
from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import Task


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

        now = datetime.now(timezone.utc)
        task.last_checked_at = now

        if task.task_type == "reminder":
            return {"ok": True, "action": "enqueue", "title": task.title, "description": task.description}

        elif task.task_type == "condition_watch":
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
