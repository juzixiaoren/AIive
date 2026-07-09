"""提醒功能的端到端测试：无需 60 秒等待，使用过期时间模拟任务触发。"""

from datetime import datetime, timedelta, timezone

from aiive.db.models import Event
from aiive.runtime.task_manager import TaskManager
from aiive.worker.task_worker import TaskWorker


class TestReminderEndToEnd:
    """测试提醒从创建到触发的完整端到端流程。"""

    def test_create_task_and_worker_fires_notification(self, db_session):
        """创建过期任务后，Worker 应触发通知事件并标记任务为完成。"""
        # 1. 创建过期任务（模拟 5 分钟之前）
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        mgr = TaskManager(db_session)
        task = mgr.create(task_type="reminder", title="hi", description="测试提醒", next_check_at=past)
        db_session.flush()

        # 2. 运行 Worker（无需真实等待）
        worker = TaskWorker(db_session)
        results = worker.poll_and_notify()
        db_session.flush()

        # 3. 任务应标记为完成
        updated = db_session.get(type(task), task.id)
        assert updated.status == "completed"

        # 4. 应存在通知事件
        notif = db_session.query(Event).filter(
            Event.event_type == "notification_created",
            Event.trace_id == task.id,
        ).first()
        assert notif is not None, "Worker 必须产生通知事件"
        assert notif.payload["title"] == "hi"

        # 5. 通知内容正确
        assert "hi" in notif.payload.get("message", "")

    def test_future_task_not_triggered(self, db_session):
        """未来的任务不应被触发。"""
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        mgr = TaskManager(db_session)
        mgr.create(task_type="reminder", title="future", next_check_at=future)
        db_session.flush()

        worker = TaskWorker(db_session)
        results = worker.poll_and_notify()
        assert len(results) == 0

    def test_multiple_due_tasks_all_fire(self, db_session):
        """多个过期任务应全部产生通知。"""
        past = datetime.now(timezone.utc) - timedelta(minutes=3)
        mgr = TaskManager(db_session)
        mgr.create("reminder", "A", next_check_at=past)
        mgr.create("reminder", "B", next_check_at=past)
        db_session.flush()

        worker = TaskWorker(db_session)
        results = worker.poll_and_notify()

        assert len(results) == 2
        for r in results:
            assert r["status"] == "triggered"
