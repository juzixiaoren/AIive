"""End-to-end reminder test: no 60s wait, uses past-time task simulation."""

from datetime import datetime, timedelta, timezone

from aiive.db.models import Event
from aiive.runtime.task_manager import TaskManager
from aiive.worker.task_worker import TaskWorker


class TestReminderEndToEnd:
    def test_create_task_and_worker_fires_notification(self, db_session):
        """Create a task with overdue next_check_at → worker should fire notification."""
        # 1. Create overdue task (simulating 5 min ago)
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        mgr = TaskManager(db_session)
        task = mgr.create(task_type="reminder", title="hi", description="测试提醒", next_check_at=past)
        db_session.flush()

        # 2. Run worker (no real wait)
        worker = TaskWorker(db_session)
        results = worker.poll_and_notify()
        db_session.flush()

        # 3. Task should be completed
        updated = db_session.get(type(task), task.id)
        assert updated.status == "completed"

        # 4. Notification event should exist
        notif = db_session.query(Event).filter(
            Event.event_type == "notification_created",
            Event.trace_id == task.id,
        ).first()
        assert notif is not None, "Worker must produce notification event"
        assert notif.payload["title"] == "hi"

        # 5. Notification content is correct
        assert "hi" in notif.payload.get("message", "")

    def test_future_task_not_triggered(self, db_session):
        """Future task should NOT fire."""
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        mgr = TaskManager(db_session)
        mgr.create(task_type="reminder", title="future", next_check_at=future)
        db_session.flush()

        worker = TaskWorker(db_session)
        results = worker.poll_and_notify()
        assert len(results) == 0

    def test_multiple_due_tasks_all_fire(self, db_session):
        """Multiple overdue tasks should all produce notifications."""
        past = datetime.now(timezone.utc) - timedelta(minutes=3)
        mgr = TaskManager(db_session)
        mgr.create("reminder", "A", next_check_at=past)
        mgr.create("reminder", "B", next_check_at=past)
        db_session.flush()

        worker = TaskWorker(db_session)
        results = worker.poll_and_notify()

        assert len(results) == 2
        for r in results:
            assert r["status"] == "notified"
