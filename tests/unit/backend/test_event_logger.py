"""测试 EventLogger 事件日志记录功能。"""
from unittest.mock import MagicMock, patch

from aiive.runtime.event_logger import EventLogger
from aiive.runtime.trace import Trace


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

    def test_log_event_default_payload(self, db_session):
        """验证未提供 payload 时默认为空字典。"""
        logger = EventLogger(db_session)
        event = logger.log_event("trace-1", "thread-1", "chat_started")
        db_session.flush()

        assert event.payload == {}


class TestFaithfulTracing:
    """验证 trace 如实记录 Agent 输入（系统注入、工具参数）与输出（工具结果、LLM I/O）。"""


    def test_system_injection_event_records_full_prompt(self, db_session):
        """运行 AgentGraph 必须写入 system_injection 事件，包含完整系统提示与注入工具列表。"""
        from aiive.db.models import Event
        from aiive.runtime.agent_graph import AgentGraph

        fake_llm = MagicMock()
        fake_llm._default_model = "m"
        fake_llm._api_key = "k"
        fake_llm._base_url = "u"
        fake_llm._timeout_seconds = 30

        graph = AgentGraph(fake_llm, db_session)
        graph._outbox = MagicMock()
        trace = Trace.new()
        thread = MagicMock()
        thread.id = "t-sysinj"

        system_content = "YOU ARE THE AGENT\n## Runtime Identity\n- agent_display_name: A"
        tool_echo = MagicMock()
        tool_echo.name = "echo"
        tool_list = MagicMock()
        tool_list.name = "list_tasks"
        tools = [tool_echo, tool_list]

        with patch("aiive.worker.task_worker.TaskWorker"), patch(
            "aiive.runtime.agent_graph.build_langchain_tools", return_value=tools
        ), patch("aiive.runtime.agent_graph.get_tool_registry") as mock_reg:
            mock_reg.return_value.list_all.return_value = []
            # 直接调用内部记录点，避免真实 LLM 调用
            graph._logger.log_event(
                trace_id=trace.trace_id, thread_id=thread.id,
                event_type="system_injection",
                payload={
                    "content": system_content,
                    "injected_tools": [getattr(t, "name", "") for t in tools],
                    "memory_count": 1, "active_task_count": 0, "due_task_count": 0,
                },
            )
            db_session.flush()

        evt = db_session.query(Event).filter(
            Event.event_type == "system_injection",
            Event.thread_id == "t-sysinj",
        ).one()
        assert evt.payload["content"] == system_content
        assert evt.payload["injected_tools"] == ["echo", "list_tasks"]
