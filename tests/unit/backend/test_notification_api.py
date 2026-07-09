from datetime import datetime, timedelta, timezone

from aiive.db.models import Event
from aiive.runtime.task_manager import TaskManager
from aiive.worker.task_worker import TaskWorker


class TestTaskWorkerEndToEnd:
    """Real end-to-end test: create task → worker polls → produces notification event."""

    def test_overdue_task_produces_notification_event(self, db_session):
        mgr = TaskManager(db_session)
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        task = mgr.create("reminder", "测试提醒", next_check_at=past)
        db_session.flush()

        # Run worker
        worker = TaskWorker(db_session)
        results = worker.poll_and_notify()
        db_session.flush()

        # Task should be completed
        updated = mgr._db.get(type(task), task.id)
        assert updated.status == "completed"

        # Notification event should exist
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
        mgr = TaskManager(db_session)
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        mgr.create("reminder", "未来提醒", next_check_at=future)
        db_session.flush()

        worker = TaskWorker(db_session)
        results = worker.poll_and_notify()

        assert len(results) == 0

    def test_condition_not_met_no_notification(self, db_session):
        mgr = TaskManager(db_session)
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        mgr.create("condition_watch", "条件检查", condition="false", next_check_at=past)
        db_session.flush()

        worker = TaskWorker(db_session)
        results = worker.poll_and_notify()

        assert len(results) == 0
