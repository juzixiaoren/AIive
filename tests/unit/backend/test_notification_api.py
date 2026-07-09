"""测试 TaskWorker 的端到端提醒通知功能。

验证从任务创建到 Worker 轮询再产生通知事件的完整流程。
"""

from datetime import datetime, timedelta, timezone

from aiive.db.models import Event
from aiive.runtime.task_manager import TaskManager
from aiive.worker.task_worker import TaskWorker


class TestTaskWorkerEndToEnd:
    """真实端到端测试：创建任务 -> Worker 轮询 -> 产生通知事件。"""

    def test_overdue_task_produces_notification_event(self, db_session):
        """过期的提醒任务经 Worker 处理后应产生通知事件并标记为完成。"""
        mgr = TaskManager(db_session)
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        task = mgr.create("reminder", "测试提醒", next_check_at=past)
        db_session.flush()

        # 运行 Worker
        worker = TaskWorker(db_session)
        results = worker.poll_and_notify()
        db_session.flush()

        # 任务应被标记为完成
        updated = mgr._db.get(type(task), task.id)
        assert updated.status == "completed"

        # 应存在通知事件
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
