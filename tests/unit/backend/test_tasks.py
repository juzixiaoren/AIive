"""测试 TaskManager：提醒生命周期、历史任务类型退役与调度边界。"""

from datetime import datetime, timedelta, timezone

import pytest

from aiive.api.routes_tasks import cancel_task, check_task
from aiive.db.models import OutboxJob, Task, Thread
from aiive.runtime.task_manager import TaskManager
from aiive.worker.task_worker import enqueue_due_tasks


def _legacy_task(db, task_type: str, *, status: str = "pending", **kwargs) -> Task:
    """直接写入历史旧类型行：模拟升级前已存在的数据（生产入口不再创建）。"""
    task = Task(task_type=task_type, title=f"历史-{task_type}", status=status, **kwargs)
    db.add(task)
    db.flush()
    return task


class TestTaskManager:
    """测试 TaskManager 的提醒创建和管理功能。"""

    def test_create_reminder(self, db_session):
        """创建提醒任务后应具有正确的类型和状态。"""
        mgr = TaskManager(db_session)
        task = mgr.create("reminder", "带伞", "明天可能下雨")
        db_session.flush()
        assert task.task_type == "reminder"
        assert task.status == "pending"

    def test_create_rejects_retired_types(self, db_session):
        """condition_watch / routine 不再允许通过生产入口创建。"""
        mgr = TaskManager(db_session)
        for task_type in ("condition_watch", "routine", "unknown"):
            with pytest.raises(ValueError, match="unsupported_task_type"):
                mgr.create(task_type, "退役类型")

    def test_list_all(self, db_session):
        """list_all 应返回所有任务（含历史旧类型）。"""
        mgr = TaskManager(db_session)
        mgr.create("reminder", "T1")
        _legacy_task(db_session, "routine")
        db_session.flush()
        assert len(mgr.list_all()) == 2

    def test_next_due_at_only_reminders_with_time(self, db_session):
        """调度只看 reminder 且 next_check_at 非空，历史类型与空时间不参与。"""
        mgr = TaskManager(db_session)
        now = datetime.now(timezone.utc)
        _legacy_task(db_session, "routine", next_check_at=now - timedelta(hours=3))
        _legacy_task(db_session, "condition_watch", next_check_at=now - timedelta(hours=2))
        reminder = mgr.create("reminder", "Due", next_check_at=now - timedelta(hours=1))
        mgr.create("reminder", "NoTime")
        db_session.flush()

        due_at = mgr.next_due_at()

        assert due_at == reminder.next_check_at

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


class TestRetiredTaskTypes:
    """历史 condition_watch / routine 行：保留数据，拒绝执行。"""

    def test_check_now_rejects_routine_without_side_effects(self, db_session):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        task = _legacy_task(db_session, "routine", condition="1440", next_check_at=past)
        before = (task.status, task.next_check_at, task.last_checked_at)

        result = TaskManager(db_session).check_now(task.id)
        db_session.flush()

        assert result["ok"] is False
        assert result["status"] == "unsupported_task_type"
        assert db_session.get(Task, task.id).status == "pending"
        assert (task.status, task.next_check_at, task.last_checked_at) == before
        assert db_session.query(OutboxJob).count() == 0

    def test_check_now_rejects_condition_watch_without_side_effects(self, db_session):
        task = _legacy_task(db_session, "condition_watch", condition="true")

        result = TaskManager(db_session).check_now(task.id)
        db_session.flush()

        assert result["status"] == "unsupported_task_type"
        assert db_session.get(Task, task.id).status == "pending"
        assert db_session.query(OutboxJob).count() == 0

    def test_historical_legacy_tasks_remain_queryable_and_cancellable(self, db_session):
        routine = _legacy_task(db_session, "routine", condition="60")
        watch = _legacy_task(db_session, "condition_watch", condition="false")
        db_session.commit()

        tasks = TaskManager(db_session).list_all()
        assert {routine.id, watch.id} <= {task.id for task in tasks}

        result = cancel_task(routine.id, db_session)
        db_session.expire_all()

        assert result["status"] == "cancelled"
        assert db_session.get(Task, routine.id).status == "cancelled"
        assert db_session.get(Task, watch.id).status == "pending"

    def test_due_scan_ignores_historical_legacy_tasks(self, db_session):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        _legacy_task(db_session, "routine", condition="60", next_check_at=past)
        _legacy_task(db_session, "condition_watch", condition="true", next_check_at=past)
        db_session.commit()

        assert enqueue_due_tasks(db_session) == []
        assert db_session.query(OutboxJob).count() == 0
