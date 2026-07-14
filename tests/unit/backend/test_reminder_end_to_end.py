"""提醒功能的端到端测试：无需 60 秒等待，使用过期时间模拟任务触发。"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from aiive.db.base import SessionLocal
from aiive.db.models import Event
from aiive.runtime.task_manager import TaskManager
from aiive.worker import task_worker
from aiive.worker.task_worker import TaskWorker


class TestReminderWakeSuccess:
    """回归测试：P0 问题 2。提醒成功唤醒后必须向线程广播消息。

    修复前模块级 _wake_agent_for_reminder 在成功 commit 后引用未定义变量
    ``task``，导致 NameError，使 broadcast_to_thread_sync 永远执行不到，
    用户收不到提醒。此处直接验证成功路径会真正广播。
    """

    def test_success_broadcasts_to_thread(self):
        """Agent 成功生成回复后，应向目标线程广播 new_message。"""

        class _FakeSession:
            def query(self, *a, **k):
                return self

            def filter(self, *a, **k):
                return self

            def order_by(self, *a, **k):
                return self

            def limit(self, n):
                return self

            def all(self):
                return []

            def close(self):
                pass

            def commit(self):
                pass

        fake_graph = MagicMock()
        fake_graph.run_runtime_event.return_value = {
            "reply": "提醒：喝水",
            "thread_id": "thread-1",
            "trace_id": "trace-1",
            "action_cards": [],
        }

        with patch.object(task_worker, "SessionLocal", lambda: _FakeSession()), \
                patch("aiive.core.llm_client.default_llm_client", MagicMock()), \
                patch("aiive.runtime.agent_graph.AgentGraph", return_value=fake_graph), \
                patch("aiive.runtime.thread_bootstrap.ThreadBootstrapService") as tb, \
                patch("aiive.api.ws_manager.ws_manager") as ws:
            tb.ensure_committed_thread.return_value = None
            task_worker._wake_agent_for_reminder(
                task_id="task-1",
                task_title="喝水提醒",
                task_type="reminder",
                target_thread_id="thread-1",
            )

        ws.broadcast_to_thread_sync.assert_called_once()
        args, kwargs = ws.broadcast_to_thread_sync.call_args
        assert args[0] == "thread-1"
        assert args[1] == "new_message"
        assert args[2]["reply"] == "提醒：喝水"


class TestReminderEndToEnd:
    """测试提醒从创建到触发的完整端到端流程。"""

    @patch("aiive.runtime.agent_graph.ChatOpenAI", side_effect=Exception("llm unavailable in test"))
    def test_create_task_and_worker_fires_notification(self, mock_chat, db_session):
        """创建过期任务后，Worker 应触发通知事件并标记任务为完成。

        LLM 不可用（强制回退路径），确保测试不依赖外部 LLM 可达性。
        """
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
        # Worker 回退路径用独立 SessionLocal() 写入，查询时也用 SessionLocal()
        lookup_db = SessionLocal()
        try:
            notif = lookup_db.query(Event).filter(
                Event.event_type == "notification_created",
                Event.trace_id == task.id,
            ).first()
        finally:
            lookup_db.close()
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
