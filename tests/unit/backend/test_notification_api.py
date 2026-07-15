"""测试 TaskWorker 的端到端提醒通知功能。

验证从任务创建到 Worker 轮询再产生通知事件的完整流程。
"""

from datetime import datetime, timedelta, timezone

import pytest

from aiive.api.routes_notifications import delete_notification
from aiive.db.base import SessionLocal
from aiive.db.models import Event, Task
from aiive.runtime.task_manager import TaskManager
from aiive.worker.task_worker import TaskWorker


class TestDeleteNotificationCancelsTask:
    """回归测试：P1 问题 5。删除通知必须同步取消关联定时任务。

    修复前 delete_notification 只物理删除 Event，关联 Task 仍为 pending，
    后台调度守护进程到期会再次触发该提醒。
    """

    def test_delete_cancels_linked_task(self, db_session):
        """删除带 task_id 的通知后，关联 Task 应被标记为 cancelled。"""
        mgr = TaskManager(db_session)
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        task = mgr.create("reminder", "喝水提醒", next_check_at=past)
        db_session.flush()
        assert task.status == "pending"

        event = Event(
            event_type="notification_created",
            thread_id="thread-1",
            trace_id=task.id,
            payload={"task_id": task.id, "title": "喝水提醒", "status": "alerting"},
        )
        db_session.add(event)
        db_session.flush()

        result = delete_notification(event.id, db_session)
        db_session.flush()

        assert result["ok"] is True
        assert result["task_cancelled"] is True
        updated = db_session.get(Task, task.id)
        assert updated.status == "cancelled"

    def test_delete_without_task_does_not_error(self, db_session):
        """没有关联 task_id 的通知也应可正常删除。"""
        event = Event(
            event_type="notification_created",
            thread_id="thread-1",
            trace_id="no-task",
            payload={"title": "无任务通知"},
        )
        db_session.add(event)
        db_session.flush()

        result = delete_notification(event.id, db_session)
        assert result["ok"] is True
        assert result["task_cancelled"] is False
        assert db_session.get(Event, event.id) is None


class TestTaskWorkerEndToEnd:
    """真实端到端测试：创建任务 -> Worker 轮询 -> 产生通知事件。"""

    def test_overdue_task_produces_notification_event(self, db_session, monkeypatch):
        """过期的提醒任务经 Worker 处理后应产生通知事件并标记为完成。"""
        # 将 SessionLocal 替换为 db_session 的工厂，避免连接外部 PostgreSQL
        monkeypatch.setattr("aiive.worker.task_worker.SessionLocal", lambda: db_session)
        monkeypatch.setattr("aiive.db.base.SessionLocal", lambda: db_session)

        mgr = TaskManager(db_session)
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        task = mgr.create("reminder", "测试提醒", next_check_at=past)
        db_session.flush()

        # 运行 Worker
        worker = TaskWorker(db_session)
        results = worker.poll_and_notify()
        db_session.flush()

        # 任务应被标记为完成
        updated = mgr._db.get(type(task), task.id)
        assert updated.status == "completed"

        # Worker fallback 使用 monkeypatched SessionLocal → 写入同一个 SQLite db_session
        notif = (
            db_session.query(Event)
            .filter(
                Event.event_type == "notification_created",
                Event.trace_id == task.id,
            )
            .first()
        )
        assert notif is not None
        assert notif.payload["title"] == "测试提醒"

    def test_future_task_not_triggered(self, db_session):
        """未来的任务不应被触发。"""
        mgr = TaskManager(db_session)
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        mgr.create("reminder", "未来提醒", next_check_at=future)
        db_session.flush()

        worker = TaskWorker(db_session)
        results = worker.poll_and_notify()

        assert len(results) == 0

    def test_condition_not_met_no_notification(self, db_session):
        """条件未满足时不应产生通知。"""
        mgr = TaskManager(db_session)
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        mgr.create("condition_watch", "条件检查", condition="false", next_check_at=past)
        db_session.flush()

        worker = TaskWorker(db_session)
        results = worker.poll_and_notify()

        assert len(results) == 0
