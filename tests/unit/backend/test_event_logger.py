"""测试 EventLogger 事件日志记录功能。"""
from aiive.runtime.event_logger import EventLogger


class TestEventLogger:
    """测试 EventLogger 的记录、查询和 LLM 调用日志功能。"""

    def test_log_event_creates_record(self, db_session):
        """验证记录事件后数据库中存在对应记录。"""
        logger = EventLogger(db_session)
        event = logger.log_event(
            trace_id="trace-1",
            thread_id="thread-1",
            event_type="user_message",
            payload={"content": "Hello"},
        )
        db_session.flush()

        assert event.id is not None
        assert event.trace_id == "trace-1"
        assert event.thread_id == "thread-1"
        assert event.event_type == "user_message"
        assert event.payload == {"content": "Hello"}
        assert event.created_at is not None

    def test_log_multiple_events(self, db_session):
        """验证多条事件可正确记录并按顺序查询。"""
        logger = EventLogger(db_session)
        logger.log_event("trace-1", "thread-1", "user_message", {"content": "A"})
        logger.log_event("trace-1", "thread-1", "llm_response", {"content": "B"})
        db_session.flush()

        from aiive.db.models import Event

        events = (
            db_session.query(Event)
            .filter(Event.thread_id == "thread-1")
            .order_by(Event.created_at.asc())
            .all()
        )
        assert len(events) == 2
        assert events[0].event_type == "user_message"
        assert events[1].event_type == "llm_response"

    def test_log_llm_call_creates_record(self, db_session):
        """验证 LLM 调用日志包含模型、延迟、输入输出预览等字段。"""
        logger = EventLogger(db_session)
        call = logger.log_llm_call(
            trace_id="trace-1",
            thread_id="thread-1",
            model="test-model",
            latency_ms=100.5,
            input_preview="in",
            output_preview="out",
        )
        db_session.flush()

        assert call.trace_id == "trace-1"
        assert call.thread_id == "thread-1"
        assert call.model == "test-model"
        assert call.latency_ms == 100.5
        assert call.input_preview == "in"
        assert call.output_preview == "out"

    def test_log_event_default_payload(self, db_session):
        """验证未提供 payload 时默认为空字典。"""
        logger = EventLogger(db_session)
        event = logger.log_event("trace-1", "thread-1", "chat_started")
        db_session.flush()

        assert event.payload == {}
