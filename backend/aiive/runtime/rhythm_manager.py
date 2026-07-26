"""
运行时层 - 节奏管理器。

负责生成 Agent 的日报和周报摘要，统计事件量、活跃例程和待办任务，
为 LLM 提供运行节奏上下文。
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from aiive.db.models import Event, MemoryRecord
from typing import Any


class RhythmManager:
    """节奏管理器：提供日度和周度的运行摘要统计。

    Attributes:
        _db: SQLAlchemy 数据库会话
    """
    def __init__(self, db: Session):
        self._db: Session = db

    def daily_summary(self) -> dict[str, Any]:
        """生成当日运行摘要。

        统计今日事件总数、活跃例程和待处理任务。

        Returns:
            包含 date、events_today、active_routines、routine_names、pending_tasks 的字典
        """
        # 用本地时区的日界（而非 UTC 零点），并清零 microsecond，
        # 保证“今日”统计与用户感知的自然日一致。
        today = datetime.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0,
        )

        events_today = (
            self._db.query(Event)
            .filter(Event.created_at >= today)
            .count()
        )

        # 从记忆表中获取活跃的例程
        routines = (
            self._db.query(MemoryRecord)
            .filter(
                MemoryRecord.memory_type.in_(["routine", "schedule", "habit"]),
                MemoryRecord.lifecycle_state == "active",
            )
            .all()
        )

        # 统计待处理任务
        from aiive.db.models import Task
        tasks = (
            self._db.query(Task)
            .filter(Task.status == "pending")
            .count()
        )

        return {
            "date": today.isoformat(),
            "events_today": events_today,
            "active_routines": len(routines),
            "routine_names": [r.content[:80] for r in routines],
            "pending_tasks": tasks,
        }

    def weekly_summary(self) -> dict[str, Any]:
        """生成最近一周的运行摘要。

        统计过去 7 天的事件总数，并返回 Top 5 事件类型分布。

        Returns:
            包含 since、total_events、top_event_types 的字典
        """
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)

        events_week = (
            self._db.query(Event)
            .filter(Event.created_at >= week_ago)
            .count()
        )

        # 本周 Top 事件类型
        from sqlalchemy import func
        top_types = (
            self._db.query(Event.event_type, func.count(Event.id))
            .filter(Event.created_at >= week_ago)
            .group_by(Event.event_type)
            .order_by(func.count(Event.id).desc())
            .limit(5)
            .all()
        )

        return {
            "since": week_ago.isoformat(),
            "total_events": events_week,
            "top_event_types": [{"type": t, "count": c} for t, c in top_types],
        }
