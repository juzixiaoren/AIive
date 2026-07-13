"""测试工具调用运行时：build_action_cards、search_memory/list_memories、AgentGraph 集成。

旧 ToolExecutor（XML 解析、去重、execute、extract_and_execute）已由 LangGraph ToolNode 替代，
相关测试已移除。
"""

import json as _json
from unittest.mock import MagicMock, patch

import pytest

from aiive.memory.recall_models import MemoryRecallPack
from aiive.runtime.tool_executor import (
    ToolCallRecord,
    build_action_cards,
)

# V2: context assembly happens in _build_agent_context. The integration tests
# below focus on response structure, so we stub it with a stable context.
FAKE_AGENT_CTX: dict = {
    "system_content": "SYSTEM",
    "identity": None,
    "policies": [],
    "core_blocks": [],
    "recall_pack": MemoryRecallPack(request_id="x"),
    "recall_traces": [],
    "recall_run_id": "run-1",
}


# ============================================================================
# 测试：build_action_cards
# ============================================================================

class TestBuildActionCards:
    """验证 build_action_cards 对各种工具执行记录的正确转换。"""

    def test_schedule_reminder_card(self):
        """schedule_reminder 完成 → task_created 卡片"""
        record = ToolCallRecord(
            name="schedule_reminder",
            params={"content": "hi", "delay_minutes": 1},
            result={"ok": True, "result": '{"reminder_set": true, "task_id": "t1", "content": "hi"}'},
            status="completed",
            trace_id="trace-1",
        )
        cards = build_action_cards([record])
        assert len(cards) == 1
        assert cards[0]["card_type"] == "task_created"

    def test_remind_alert_card(self):
        """remind_alert 完成 → reminder_alert 卡片"""
        record = ToolCallRecord(
            name="remind_alert",
            params={},
            result={"ok": True, "result": '{"reminder_id": "r1", "content": "test"}'},
            status="completed",
            trace_id="trace-1",
        )
        cards = build_action_cards([record])
        assert len(cards) == 1
        assert cards[0]["card_type"] == "reminder_alert"
        assert cards[0]["reminder_id"] == "r1"

    def test_blocked_card(self):
        """blocked 状态 → tool_blocked 卡片"""
        record = ToolCallRecord(
            name="blocked_tool",
            params={},
            result={},
            status="blocked",
            trace_id="trace-1",
            reason="Not allowed",
        )
        cards = build_action_cards([record])
        assert len(cards) == 1
        assert cards[0]["card_type"] == "tool_blocked"

    def test_parse_error_card(self):
        """parse_error 状态 → tool_error 卡片"""
        record = ToolCallRecord(
            name="bad_tool",
            params={},
            result={},
            status="parse_error",
            trace_id="trace-1",
            reason="JSON parse error",
        )
        cards = build_action_cards([record])
        assert len(cards) == 1
        assert cards[0]["card_type"] == "tool_error"

    def test_completed_tool_result(self):
        """completed 状态 → tool_result 卡片"""
        record = ToolCallRecord(
            name="echo",
            params={"message": "hello"},
            result={"ok": True, "result": "hello"},
            status="completed",
            trace_id="trace-1",
        )
        cards = build_action_cards([record])
        assert len(cards) == 1
        assert cards[0]["card_type"] == "tool_result"

    def test_empty_records(self):
        """空记录 → 空列表"""
        cards = build_action_cards([])
        assert cards == []


# ============================================================================
# 测试：memory_search（V2 Agent-Initiated Recall）
# ============================================================================

class TestMemorySearchEmptyQuery:
    """V2：memory_search("") 不循环；空查询被拒绝并附提示（需合法 RunContext）。"""

    def test_empty_query_search_returns_empty_list(self):
        """空查询返回空列表而非错误。"""
        from aiive.context.run_context import RunContext
        from aiive.tools.registry import get_tool_registry

        registry = get_tool_registry()
        ctx = RunContext(thread_id="t1", trace_id="tr1")
        result = registry.execute("memory_search", {"query": ""}, "trusted_user_command", run_context=ctx)
        assert isinstance(result, dict)
        assert result.get("ok") is True
        inner = result.get("result", {})
        if isinstance(inner, str):
            import json
            inner = json.loads(inner)
        assert inner.get("results", []) == [] or inner.get("hint")


# ============================================================================
# 测试：AgentGraph 工具集成（简化版）
# ============================================================================

class TestAgentGraphToolIntegration:
    """需求 1、4：AgentGraph.run() 返回结构正确，无 tool_call 标签泄露。"""

    @patch("aiive.runtime.agent_graph.ChatOpenAI")
    @patch("aiive.runtime.agent_graph.build_action_cards", return_value=[])
    @patch("aiive.runtime.agent_graph.AgentGraph._build_graph")
    @patch("aiive.runtime.agent_graph.AgentGraph._build_agent_context", return_value=FAKE_AGENT_CTX)
    def test_run_never_returns_tool_call_tags(self, mock_ctx, mock_graph, mock_cards, mock_chat):
        """用户可见回复绝不能包含 <tool_call> 标签。"""
        from aiive.runtime.agent_graph import AgentGraph

        mock_graph.return_value = (MagicMock(), [])
        mock_llm = MagicMock()
        mock_llm._default_model = "gpt-4"
        mock_llm._api_key = "sk-test"
        mock_llm._base_url = "https://api.openai.com/v1"
        mock_llm._timeout_seconds = 30

        # Need to also mock the compiled graph's invoke
        mock_compiled = MagicMock()
        mock_compiled.invoke.return_value = {"messages": [MagicMock(content="Hello, how can I help?", tool_calls=None)]}
        mock_graph.return_value = (mock_compiled, [])

        graph = AgentGraph(mock_llm, MagicMock())
        result = graph.run(message="hi")

        reply = result.get("reply", "")
        assert "<tool_call>" not in reply
        assert "</tool_call>" not in reply
        assert "thread_id" in result
        assert "trace_id" in result

    @patch("aiive.runtime.agent_graph.ChatOpenAI")
    @patch("aiive.runtime.agent_graph.build_action_cards", return_value=[])
    @patch("aiive.runtime.agent_graph.AgentGraph._build_graph")
    @patch("aiive.runtime.agent_graph.AgentGraph._build_agent_context", return_value=FAKE_AGENT_CTX)
    def test_run_returns_structured_response(self, mock_ctx, mock_graph, mock_cards, mock_chat):
        """run() 返回的 dict 包含必要的结构字段。"""
        from aiive.runtime.agent_graph import AgentGraph

        mock_compiled = MagicMock()
        mock_compiled.invoke.return_value = {"messages": [MagicMock(content="Done.", tool_calls=None)]}
        mock_graph.return_value = (mock_compiled, [])

        mock_llm = MagicMock()
        mock_llm._default_model = "gpt-4"
        mock_llm._api_key = "sk-test"
        mock_llm._base_url = "https://api.openai.com/v1"
        mock_llm._timeout_seconds = 30

        graph = AgentGraph(mock_llm, MagicMock())
        result = graph.run(message="test")

        assert "reply" in result
        assert "thread_id" in result
        assert "trace_id" in result
        assert "action_cards" in result
        assert isinstance(result["action_cards"], list)
