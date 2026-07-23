"""测试提醒通知与到期任务入队功能。

验证从任务创建到 Worker 轮询再产生通知事件的完整流程。
"""

from datetime import datetime, timedelta, timezone

import pytest

from aiive.api.routes_notifications import delete_notification, list_notifications
from aiive.context.run_context import RunContext
from aiive.db.models import Event, Task, Thread
from aiive.tools.builtin_tools import _handle_schedule_reminder
from aiive.runtime.task_manager import TaskManager
from aiive.worker.task_worker import enqueue_due_tasks


class TestReminderNotificationInbox:
    """提醒创建后应立即进入通知收件箱。"""

    def test_schedule_reminder_is_listed_as_pending_notification(self, db_session, monkeypatch):
        """创建提醒必须同时创建可被未执行列表读取的通知事件。"""
        import aiive.api.routes_notifications as notifications

        thread = Thread(id="reminder-notification-thread", title="通知测试")
        db_session.add(thread)
        db_session.flush()
        monkeypatch.setattr(notifications, "broadcast_pending_count", lambda db: None)

        result = _handle_schedule_reminder._aiive_db_handler(
            db_session,
            RunContext(thread_id=thread.id, trace_id="notification-trace", source="test"),
            content="喝水",
            delay_minutes=15,
        )
        db_session.flush()

        notifications = list_notifications(category="pending", db=db_session)

        assert result["reminder_set"] is True
        assert len(notifications) == 1
        assert notifications[0]["event_type"] == "reminder_created"
        assert notifications[0]["status"] == "pending"
        assert notifications[0]["title"] == "喝水"


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

    def test_delete_cancels_dispatching_task_and_pending_outbox(self, db_session):
        """删除 dispatching 提醒必须同时取消 Task 与尚未 claim 的 Outbox。"""
        from aiive.db.models import OutboxJob

        mgr = TaskManager(db_session)
        task = mgr.create("reminder", "投递中提醒")
        task.status = "dispatching"
        db_session.flush()
        job = OutboxJob(
            operation_id=f"reminder_delivery:{task.id}",
            job_type="reminder_delivery",
            status="pending",
            payload={"task_id": task.id},
            max_retries=3,
        )
        event = Event(
            event_type="notification_created",
            thread_id="thread-1",
            trace_id=task.id,
            payload={"task_id": task.id, "title": task.title, "status": "alerting"},
        )
        db_session.add_all([job, event])
        db_session.flush()

        result = delete_notification(event.id, db_session)
        db_session.expire_all()

        assert result["task_cancelled"] is True
        assert db_session.get(Task, task.id).status == "cancelled"
        assert db_session.get(OutboxJob, job.id).status == "cancelled"
        assert db_session.get(OutboxJob, job.id).terminal_reason == "task_cancelled"

    def test_delete_completed_task_only_dismisses_notification(self, db_session):
        """已完成提醒无法撤回投递，删除只隐藏通知且不得伪称取消。"""
        mgr = TaskManager(db_session)
        task = mgr.create("reminder", "已完成提醒")
        task.status = "completed"
        db_session.flush()
        event = Event(
            event_type="notification_created",
            thread_id="thread-1",
            trace_id=task.id,
            payload={"task_id": task.id, "title": task.title, "status": "confirmed"},
        )
        db_session.add(event)
        db_session.flush()

        result = delete_notification(event.id, db_session)

        assert result["task_cancelled"] is False
        assert task.status == "completed"
        assert event.payload["dismissed"] is True

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


class TestDueTaskEnqueue:
    """扫描入口测试：创建任务后由生产入口原子入队。"""

    def test_overdue_task_enqueues_delivery(self, db_session):
        """过期提醒只能进入 dispatching，不能由旧 Worker 伪造通知或直接完成。"""
        from aiive.db.models import OutboxJob

        mgr = TaskManager(db_session)
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        task = mgr.create("reminder", "测试提醒", next_check_at=past)
        db_session.flush()

        results = enqueue_due_tasks(db_session)
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

        results = enqueue_due_tasks(db_session)

        assert len(results) == 0

    def test_condition_not_met_no_notification(self, db_session):
        """条件未满足时不应产生通知。"""
        mgr = TaskManager(db_session)
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        mgr.create("condition_watch", "条件检查", condition="false", next_check_at=past)
        db_session.flush()

        results = enqueue_due_tasks(db_session)

        assert len(results) == 0
