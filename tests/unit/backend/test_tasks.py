from datetime import datetime, timedelta, timezone

from aiive.runtime.task_manager import TaskManager


class TestTaskManager:
    def test_create_reminder(self, db_session):
        mgr = TaskManager(db_session)
        task = mgr.create("reminder", "带伞", "明天可能下雨")
        db_session.flush()
        assert task.task_type == "reminder"
        assert task.status == "pending"

    def test_list_all(self, db_session):
        mgr = TaskManager(db_session)
        mgr.create("reminder", "T1")
        mgr.create("routine", "T2")
        db_session.flush()
        assert len(mgr.list_all()) == 2

    def test_get_due(self, db_session):
        mgr = TaskManager(db_session)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        mgr.create("reminder", "Due", next_check_at=past)
        db_session.flush()
        due = mgr.get_due()
        assert len(due) == 1

    def test_check_reminder_completes(self, db_session):
        mgr = TaskManager(db_session)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        task = mgr.create("reminder", "Test", next_check_at=past)
        db_session.flush()

        result = mgr.check_now(task.id)
        assert result["action"] == "notify"

        updated = mgr._db.get(type(task), task.id)
        assert updated.status == "completed"

    def test_condition_watch_skip(self, db_session):
        mgr = TaskManager(db_session)
        task = mgr.create("condition_watch", "Check weather", condition="false")
        db_session.flush()

        result = mgr.check_now(task.id)
        assert result["action"] == "skip"


class TestConditionWatch:
    def test_condition_met_triggers_notify(self, db_session):
        mgr = TaskManager(db_session)
        task = mgr.create("condition_watch", "Watch", condition="true")
        db_session.flush()
        result = mgr.check_now(task.id)
        assert result["action"] == "notify"
