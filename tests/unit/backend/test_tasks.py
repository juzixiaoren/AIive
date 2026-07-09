"""测试 TaskManager（任务管理器）和条件监视（ConditionWatch）模块。

覆盖提醒创建、列出、到期检查、条件评估等功能。
"""

from datetime import datetime, timedelta, timezone

from aiive.runtime.task_manager import TaskManager


class TestTaskManager:
    """测试 TaskManager 的任务创建和管理功能。"""

    def test_create_reminder(self, db_session):
        """创建提醒任务后应具有正确的类型和状态。"""
        mgr = TaskManager(db_session)
        task = mgr.create("reminder", "带伞", "明天可能下雨")
        db_session.flush()
        assert task.task_type == "reminder"
        assert task.status == "pending"

    def test_list_all(self, db_session):
        """list_all 应返回所有任务。"""
        mgr = TaskManager(db_session)
        mgr.create("reminder", "T1")
        mgr.create("routine", "T2")
        db_session.flush()
        assert len(mgr.list_all()) == 2

    def test_get_due(self, db_session):
        """get_due 应返回到期的任务。"""
        mgr = TaskManager(db_session)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        mgr.create("reminder", "Due", next_check_at=past)
        db_session.flush()
        due = mgr.get_due()
        assert len(due) == 1

    def test_check_reminder_completes(self, db_session):
        """到期提醒经 check_now 检查后应完成。"""
        mgr = TaskManager(db_session)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        task = mgr.create("reminder", "Test", next_check_at=past)
        db_session.flush()

        result = mgr.check_now(task.id)
        assert result["action"] == "notify"

        updated = mgr._db.get(type(task), task.id)
        assert updated.status == "completed"

    def test_condition_watch_skip(self, db_session):
        """条件为 false 时应跳过。"""
        mgr = TaskManager(db_session)
        task = mgr.create("condition_watch", "Check weather", condition="false")
        db_session.flush()

        result = mgr.check_now(task.id)
        assert result["action"] == "skip"


class TestConditionWatch:
    """测试条件监视任务的条件评估逻辑。"""

    def test_condition_met_triggers_notify(self, db_session):
        """条件满足时应触发通知。"""
        mgr = TaskManager(db_session)
        task = mgr.create("condition_watch", "Watch", condition="true")
        db_session.flush()
        result = mgr.check_now(task.id)
        assert result["action"] == "notify"
