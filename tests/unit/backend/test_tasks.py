"""测试 TaskManager（任务管理器）和条件监视（ConditionWatch）模块。

覆盖提醒创建、列出、到期检查、条件评估等功能。
"""

from datetime import datetime, timedelta, timezone

from aiive.api.routes_tasks import cancel_task, check_task
from aiive.db.models import OutboxJob, Thread
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

    def test_check_reminder_requires_enqueue(self, db_session):
        """check_now 不得在 Agent 回复前提前完成提醒。"""
        mgr = TaskManager(db_session)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        task = mgr.create("reminder", "Test", next_check_at=past)
        db_session.flush()

        result = mgr.check_now(task.id)
        assert result["status"] == "enqueued"

        updated = mgr._db.get(type(task), task.id)
        assert updated.status == "dispatching"
        assert db_session.query(OutboxJob).filter(
            OutboxJob.operation_id == f"reminder_delivery:{task.id}"
        ).count() == 1

    def test_check_now_route_enqueues_reminder(self, db_session):
        """check-now 路由必须真实入队，不能只返回自然语言动作。"""
        thread = Thread(id="check-now-thread", title="check-now")
        db_session.add(thread)
        mgr = TaskManager(db_session)
        task = mgr.create("reminder", "立即提醒")
        task.thread_id = thread.id
        db_session.commit()

        result = check_task(task.id, db_session)
        db_session.expire_all()

        assert result["status"] == "enqueued"
        assert db_session.get(type(task), task.id).status == "dispatching"
        assert db_session.query(OutboxJob).filter(
            OutboxJob.operation_id == f"reminder_delivery:{task.id}"
        ).count() == 1

    def test_check_now_does_not_return_another_due_task(self, db_session):
        """立即执行必须只处理目标提醒，不能返回更早到期的其他任务。"""
        mgr = TaskManager(db_session)
        earlier = mgr.create(
            "reminder", "更早提醒",
            next_check_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        target = mgr.create(
            "reminder", "目标提醒",
            next_check_at=datetime.now(timezone.utc) + timedelta(hours=2),
        )
        db_session.flush()

        result = mgr.enqueue_reminder_now(target.id)

        assert result["task_id"] == target.id
        assert result["status"] == "enqueued"
        assert earlier.status == "pending"
        assert target.status == "dispatching"

    def test_check_now_does_not_revive_terminal_reminder(self, db_session):
        """completed/cancelled/failed 提醒不得被 check-now 复活。"""
        mgr = TaskManager(db_session)
        for status in ("completed", "cancelled", "failed"):
            task = mgr.create("reminder", f"终态-{status}")
            task.status = status
            db_session.flush()

            result = mgr.enqueue_reminder_now(task.id)

            assert task.status == status
            assert result["status"] in {"already_completed", "cancelled", "failed"}
            assert db_session.query(OutboxJob).filter(
                OutboxJob.operation_id == f"reminder_delivery:{task.id}"
            ).count() == 0

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
