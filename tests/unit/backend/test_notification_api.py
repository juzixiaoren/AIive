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
        retained = db_session.get(Event, event.id)
        assert retained is not None
        assert retained.payload["dismissed"] is True

    def test_delete_updates_all_linked_reminders_beyond_recent_fifty(self, db_session):
        """删除通知时应由数据库定位并隐藏同一任务的全部关联提醒。"""
        mgr = TaskManager(db_session)
        task = mgr.create("reminder", "长期提醒")
        db_session.flush()
        linked = Event(
            event_type="reminder_created",
            thread_id="thread-1",
            trace_id="linked",
            payload={"task_id": task.id, "status": "pending"},
        )
        db_session.add(linked)
        for index in range(60):
            db_session.add(Event(
                event_type="reminder_created",
                thread_id="thread-1",
                trace_id=f"other-{index}",
                payload={"task_id": f"other-{index}", "status": "pending"},
            ))
        db_session.flush()

        delete_notification(linked.id, db_session)
        db_session.refresh(linked)

        assert linked.payload["status"] == "cancelled"
        assert linked.payload["dismissed"] is True

    def test_delete_rejects_non_notification_event(self, db_session):
        """通知接口不得成为删除任意审计事件的入口。"""
        from fastapi import HTTPException

        event = Event(
            event_type="user_message",
            thread_id="thread-1",
            trace_id="trace-1",
            payload={"content": "必须保留"},
        )
        db_session.add(event)
        db_session.flush()

        with pytest.raises(HTTPException) as exc_info:
            delete_notification(event.id, db_session)

        assert exc_info.value.status_code == 404
        assert db_session.get(Event, event.id) is not None


class TestTaskWorkerEndToEnd:
    """扫描入口测试：创建任务 -> Worker 原子入队。"""

    def test_overdue_task_enqueues_delivery(self, db_session):
        """过期提醒只能进入 dispatching，不能由旧 Worker 伪造通知或直接完成。"""
        from aiive.db.models import OutboxJob

        mgr = TaskManager(db_session)
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        task = mgr.create("reminder", "测试提醒", next_check_at=past)
        db_session.flush()

        results = TaskWorker(db_session).poll_and_notify()
        db_session.flush()

        updated = mgr._db.get(type(task), task.id)
        assert updated.status == "dispatching"
        assert results[0]["status"] == "dispatching"
        assert db_session.query(OutboxJob).filter(
            OutboxJob.operation_id == f"reminder_delivery:{task.id}"
        ).count() == 1
        assert db_session.query(Event).filter(
            Event.event_type == "notification_created",
            Event.trace_id == task.id,
        ).count() == 0

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
