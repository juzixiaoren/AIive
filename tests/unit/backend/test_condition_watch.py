from datetime import datetime, timedelta, timezone

from aiive.runtime.task_manager import TaskManager


class TestRoutineTask:
    def test_routine_notifies_and_reschedules(self, db_session):
        mgr = TaskManager(db_session)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        task = mgr.create("routine", "每日站会", condition="1440", next_check_at=past)
        db_session.flush()

        result = mgr.check_now(task.id)
        assert result["action"] == "notify"

        updated = mgr._db.get(type(task), task.id)
        assert updated.status == "pending"  # rescheduled

    def test_not_due_not_returned(self, db_session):
        mgr = TaskManager(db_session)
        future = datetime.now(timezone.utc) + timedelta(hours=24)
        mgr.create("reminder", "Future", next_check_at=future)
        db_session.flush()

        due = mgr.get_due()
        assert len(due) == 0
