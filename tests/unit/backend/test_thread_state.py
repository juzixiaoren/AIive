"""测试 ThreadState（线程状态管理）模块。

覆盖线程创建、获取历史消息、消息排序及消息数量限制等功能。
"""

from aiive.runtime.event_logger import EventLogger
from aiive.runtime.thread_state import ThreadState


class TestThreadState:
    """测试 ThreadState 的线程和消息管理功能。"""

    def test_get_or_create_creates_new_thread(self, db_session):
        """不传 ID 时应创建新线程。"""
        state = ThreadState(db_session)
        thread = state.get_or_create_thread()
        db_session.flush()

        assert thread.id is not None
        assert thread.created_at is not None

    def test_get_or_create_returns_existing(self, db_session):
        """传入已存在 ID 时应返回已有线程。"""
        state = ThreadState(db_session)
        t1 = state.get_or_create_thread()
        db_session.flush()

        t2 = state.get_or_create_thread(thread_id=t1.id)
        assert t2.id == t1.id

    def test_get_or_create_new_when_not_found(self, db_session):
        """传入不存在的 ID 时应创建新线程（ID 被替换）。"""
        state = ThreadState(db_session)
        thread = state.get_or_create_thread(thread_id="nonexistent-id")
        db_session.flush()

        assert thread.id != "nonexistent-id"

    def test_get_recent_messages_empty(self, db_session):
        """空线程应返回空消息列表。"""
        state = ThreadState(db_session)
        thread = state.get_or_create_thread()
        db_session.flush()

        messages = state.get_recent_messages(thread.id)
        assert messages == []

    def test_get_recent_messages_returns_ordered(self, db_session):
        """消息应按时间顺序返回，且包含正确的类型映射。"""
        logger = EventLogger(db_session)
        state = ThreadState(db_session)
        thread = state.get_or_create_thread()
        db_session.flush()

        logger.log_event("t1", thread.id, "user_message", {"content": "Hi"})
        logger.log_event("t1", thread.id, "llm_response", {"content": "Hello!"})
        logger.log_event("t2", thread.id, "user_message", {"content": "How are you?"})
        logger.log_event("t2", thread.id, "llm_response", {"content": "Good!"})
        # 非消息事件应被跳过
        logger.log_event("t3", thread.id, "chat_started", {})
        db_session.flush()

        messages = state.get_recent_messages(thread.id)
        assert len(messages) == 4
        assert messages[0]["type"] == "user"
        assert messages[0]["content"] == "Hi"
        assert messages[1]["type"] == "assistant"
        assert messages[1]["content"] == "Hello!"
        assert messages[2]["content"] == "How are you?"
        assert messages[3]["content"] == "Good!"

    def test_get_recent_messages_respects_limit(self, db_session):
        """消息数量应受 limit 参数限制。"""
        logger = EventLogger(db_session)
        state = ThreadState(db_session)
        thread = state.get_or_create_thread()
        db_session.flush()

        for i in range(10):
            logger.log_event(f"t{i}", thread.id, "user_message", {"content": f"msg{i}"})

        messages = state.get_recent_messages(thread.id, limit=5)
        assert len(messages) == 5

    def test_messages_skip_empty_content(self, db_session):
        """空内容的消息应被跳过。"""
        logger = EventLogger(db_session)
        state = ThreadState(db_session)
        thread = state.get_or_create_thread()
        db_session.flush()

        logger.log_event("t1", thread.id, "user_message", {"content": ""})
        db_session.flush()

        messages = state.get_recent_messages(thread.id)
        assert messages == []
