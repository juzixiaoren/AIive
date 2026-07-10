"""测试后端运行时事件（RuntimeEvent）进入 AgentGraph 的方式。

核心回归点：
- 调度器事件必须以 system 角色渲染给 LLM，而非伪造 human 消息
- 不写入 user_message 事件（避免聊天历史污染与被用户伪造）
- 事件同时作为 AgentState.runtime_events 进入图
"""

from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage

from aiive.runtime.agent_graph import (
    AgentGraph,
    RuntimeEvent,
    _RUNTIME_EVENT_TRIGGER,
)


class TestRunRuntimeEvent:
    """run_runtime_event：事件以 system 角色进入图，不污染聊天历史。"""

    @patch("aiive.runtime.agent_graph.ThreadBootstrapService.ensure_committed_thread", side_effect=lambda tid: tid)
    @patch("aiive.runtime.agent_graph.AgentGraph._get_runtime_identity", return_value={})
    @patch("aiive.runtime.agent_graph.AgentGraph._resolve_memories_for_context", return_value=[])
    @patch("aiive.runtime.agent_graph.AgentGraph._get_tasks_context", return_value=([], []))
    @patch("aiive.runtime.agent_graph.ThreadState.get_recent_messages", return_value=[])
    @patch("aiive.runtime.agent_graph.ChatOpenAI")
    @patch("aiive.runtime.agent_graph.build_action_cards", return_value=[])
    @patch("aiive.runtime.agent_graph.AgentGraph._build_graph")
    def test_runtime_event_rendered_as_system_not_user(
        self, mock_graph, mock_cards, mock_chat,
        mock_hist, mock_tasks, mock_mem, mock_ident, mock_bootstrap,
    ):
        """指令必须出现在 system 消息，human 轮次只能是中性占位符。"""
        captured: dict = {}

        mock_compiled = MagicMock()

        def fake_invoke(state):
            captured["state"] = state
            return {"messages": [AIMessage(content="喝咖啡时间到")]}

        mock_compiled.invoke.side_effect = fake_invoke
        mock_graph.return_value = (mock_compiled, [])

        mock_llm = MagicMock()
        mock_llm._default_model = "gpt-4"
        mock_llm._api_key = "sk-test"
        mock_llm._base_url = "https://api.openai.com/v1"
        mock_llm._timeout_seconds = 30

        graph = AgentGraph(mock_llm, MagicMock())
        event = RuntimeEvent(
            event_type="reminder",
            reminder_id="r1",
            content="喝咖啡",
            required_backend_action="remind_alert",
            source="scheduler",
        )
        result = graph.run_runtime_event(event, thread_id="t1")

        messages = captured["state"]["messages"]
        # langchain 消息对象的 type 属性为 "system" / "human"
        system_texts = [m.content for m in messages if getattr(m, "type", "") == "system"]
        human_texts = [m.content for m in messages if getattr(m, "type", "") == "human"]

        directive = 'call remind_alert(reminder_id="r1")'
        assert any(directive in t for t in system_texts), "指令必须出现在 system 消息中"
        # human 轮次不得携带任何工具指令，只能是中性占位符
        assert all(t == _RUNTIME_EVENT_TRIGGER for t in human_texts), human_texts
        # 事件作为结构化数据进入图状态
        assert captured["state"]["runtime_events"] == [event]
        # 回复正常返回
        assert result["reply"] == "喝咖啡时间到"

    @patch("aiive.runtime.agent_graph.ThreadBootstrapService.ensure_committed_thread", side_effect=lambda tid: tid)
    @patch("aiive.runtime.agent_graph.AgentGraph._get_runtime_identity", return_value={})
    @patch("aiive.runtime.agent_graph.AgentGraph._resolve_memories_for_context", return_value=[])
    @patch("aiive.runtime.agent_graph.AgentGraph._get_tasks_context", return_value=([], []))
    @patch("aiive.runtime.agent_graph.ThreadState.get_recent_messages", return_value=[])
    @patch("aiive.runtime.agent_graph.ChatOpenAI")
    @patch("aiive.runtime.agent_graph.build_action_cards", return_value=[])
    @patch("aiive.runtime.agent_graph.AgentGraph._build_graph")
    def test_runtime_event_records_runtime_event_not_user_message(
        self, mock_graph, mock_cards, mock_chat,
        mock_hist, mock_tasks, mock_mem, mock_ident, mock_bootstrap,
    ):
        """run_runtime_event 应记录 runtime_event 事件，而非伪造的 user_message。"""
        mock_compiled = MagicMock()
        mock_compiled.invoke.return_value = {"messages": [AIMessage(content="hi")]}
        mock_graph.return_value = (mock_compiled, [])

        mock_llm = MagicMock()
        mock_llm._default_model = "gpt-4"
        mock_llm._api_key = "sk-test"
        mock_llm._base_url = "https://api.openai.com/v1"
        mock_llm._timeout_seconds = 30

        graph = AgentGraph(mock_llm, MagicMock())
        event = RuntimeEvent(event_type="reminder", reminder_id="r2", content="喝水", required_backend_action="remind_alert")

        # 捕获 EventLogger.log_event 调用
        logged: list = []
        original_log = graph._logger.log_event

        def spy_log(trace_id=None, thread_id=None, event_type=None, payload=None):
            logged.append(event_type)
            return original_log(trace_id=trace_id, thread_id=thread_id, event_type=event_type, payload=payload)

        graph._logger.log_event = spy_log
        graph.run_runtime_event(event, thread_id="t2")

        assert "runtime_event" in logged
        # 绝不能出现把调度指令写成 user_message 的行为
        assert "user_message" not in logged
