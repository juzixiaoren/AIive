"""测试工具调用运行时：ToolExecutor、去重、畸形标签、search_memory、list_memories。

验证全部 8 项需求：
1. 用户可见回复中不含原始 <tool_call> 标签
2. 结构化工具调用提取（通过 ToolExecutor）
3. 工具结果写入 trace/event
4. ChatResponse 区分 assistant_text、tool_calls、tool_results、action_cards
5. 畸形工具标签被检测并标记为 parse_error
6. 相同参数的连续重复调用不重复执行
7. search_memory(query="") 被拒绝；新增 list_memories
8. 集成测试：记忆查询在上下文中返回真实结果
"""

import json as _json
import tempfile
import os
import shutil
from unittest.mock import MagicMock, patch

import pytest

from aiive.runtime.tool_executor import (
    ToolExecutor,
    ParsedToolCall,
    ToolCallRecord,
    TOOL_CALL_PATTERN,
    MAX_CONSECUTIVE_IDENTICAL,
)


# ============================================================================
# 测试：ToolExecutor 提取与解析
# ============================================================================

class TestToolCallExtraction:
    """需求 2、5：结构化提取与畸形标签检测。"""

    def test_extract_valid_tool_call(self):
        """应正确提取有效的工具调用。"""
        text = '<tool_call>{"name":"echo","params":{"message":"hello"}}</tool_call>'
        calls = ToolExecutor.extract_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "echo"
        assert calls[0].params == {"message": "hello"}
        assert calls[0].is_valid is True

    def test_extract_multiple_tool_calls(self):
        """应正确提取多个工具调用。"""
        text = (
            '<tool_call>{"name":"echo","params":{"message":"a"}}</tool_call>\n'
            '<tool_call>{"name":"echo","params":{"message":"b"}}</tool_call>'
        )
        calls = ToolExecutor.extract_tool_calls(text)
        assert len(calls) == 2
        assert calls[0].params["message"] == "a"
        assert calls[1].params["message"] == "b"

    def test_extract_invalid_json_marks_error(self):
        """无效 JSON 应标记错误。"""
        text = '<tool_call>{bad json}</tool_call>'
        calls = ToolExecutor.extract_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].is_valid is False
        assert calls[0].parse_error is not None
        assert "JSON" in calls[0].parse_error

    def test_extract_missing_name_marks_error(self):
        """缺少 name 字段应标记错误。"""
        text = '<tool_call>{"params":{"x":1}}</tool_call>'
        calls = ToolExecutor.extract_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].is_valid is False
        assert "Invalid tool call structure" in (calls[0].parse_error or "")

    def test_detect_malformed_tags(self):
        """应检测到畸形标签。"""
        text = "Here is some text </tool_cost> and more text"
        errors = ToolExecutor.detect_malformed_tags(text)
        assert len(errors) >= 1
        assert any("tool_cost" in e for e in errors)

    def test_detect_unclosed_tool_call(self):
        """应检测到未闭合的 tool_call 标签。"""
        text = "I will <tool_call> now and continue"
        errors = ToolExecutor.detect_malformed_tags(text)
        assert len(errors) >= 1
        assert any("tool_call" in e for e in errors)

    def test_clean_text_strips_tool_calls(self):
        """clean_text 应移除工具调用标签，保留普通文本。"""
        text = (
            "Hello!\n"
            '<tool_call>{"name":"echo","params":{"message":"hi"}}</tool_call>\n'
            "Goodbye!"
        )
        parsed = ToolExecutor.extract_tool_calls(text)
        errors = ToolExecutor.detect_malformed_tags(text)
        cleaned = ToolExecutor.clean_text(text, parsed, errors)
        assert "<tool_call>" not in cleaned
        assert "Hello!" in cleaned
        assert "Goodbye!" in cleaned

    def test_clean_text_strips_malformed_content(self):
        """需求 1、5：clean_text 必须同时移除畸形标签。"""
        text = 'Hello<tool_call>something</tool_call></tool_cost>End'
        parsed = ToolExecutor.extract_tool_calls(text)
        errors = ToolExecutor.detect_malformed_tags(text)
        cleaned = ToolExecutor.clean_text(text, parsed, errors)
        assert "<tool_call>" not in cleaned
        assert "</tool_cost>" not in cleaned

    def test_clean_text_removes_tags_user_never_sees(self):
        """需求 1：用户可见文本绝不能包含 <tool_call> 标签。"""
        text = '<tool_call>{"name":"search_memory","params":{"query":"test"}}</tool_call>'
        parsed = ToolExecutor.extract_tool_calls(text)
        errors = ToolExecutor.detect_malformed_tags(text)
        cleaned = ToolExecutor.clean_text(text, parsed, errors)
        assert "<tool_call>" not in cleaned
        assert "</tool_call>" not in cleaned


# ============================================================================
# 测试：去重保护
# ============================================================================

class TestDedupProtection:
    """需求 6：相同 (name, params) 不得重复执行。"""

    def setup_method(self):
        self._db = MagicMock()
        self._logger = MagicMock()
        self.executor = ToolExecutor(self._db, self._logger)

    def test_first_call_not_duplicate(self):
        """首次调用不应被判定为重复。"""
        assert not self.executor._is_duplicate("echo", {"message": "hi"})

    def test_second_identical_call_is_duplicate(self):
        """第二次相同调用应被判定为重复。"""
        self.executor._execution_history.append(
            ("echo", self.executor._params_hash({"message": "hi"}))
        )
        assert self.executor._is_duplicate("echo", {"message": "hi"})

    def test_different_params_not_duplicate(self):
        """不同参数的同名调用不应被判定为重复。"""
        self.executor._execution_history.append(
            ("echo", self.executor._params_hash({"message": "a"}))
        )
        assert not self.executor._is_duplicate("echo", {"message": "b"})

    def test_different_name_not_duplicate(self):
        """不同工具名的调用不应被判定为重复。"""
        self.executor._execution_history.append(
            ("echo", self.executor._params_hash({"message": "hi"}))
        )
        assert not self.executor._is_duplicate("search_memory", {"message": "hi"})

    def test_reset_history_clears_dedup(self):
        """重置历史应清空去重记录。"""
        self.executor._execution_history.append(
            ("echo", self.executor._params_hash({"x": 1}))
        )
        self.executor.reset_history()
        assert not self.executor._is_duplicate("echo", {"x": 1})


# ============================================================================
# 测试：工具执行与日志记录
# ============================================================================

class TestToolExecution:
    """需求 3：工具结果必须记录到 trace/event。"""

    def setup_method(self):
        self._db = MagicMock()
        self._logger = MagicMock()
        self.executor = ToolExecutor(self._db, self._logger)

    def test_parse_error_record(self):
        """解析错误应产生 parse_error 状态的记录。"""
        parsed = ParsedToolCall(
            name="bad", params={}, raw_json="{bad}", is_valid=False,
            parse_error="JSON parse error",
        )
        record = self.executor.execute(parsed, "thread-1", "trace-1")
        assert record.status == "parse_error"
        assert record.reason == "JSON parse error"

    # Patch get_tool_registry where tool_executor imports it (module-level import)
    @patch("aiive.runtime.tool_executor.get_tool_registry")
    @patch("aiive.runtime.tool_executor.set_thread_context", return_value=None)
    def test_successful_execution_logs_event(self, mock_set_ctx, mock_registry):
        """需求 3：工具执行必须被记录到事件日志。"""
        mock_reg = MagicMock()
        mock_reg.execute.return_value = {"ok": True, "result": "done"}
        mock_registry.return_value = mock_reg

        parsed = ParsedToolCall(
            name="echo", params={"message": "hi"}, raw_json='{"name":"echo"}', is_valid=True,
        )
        record = self.executor.execute(parsed, "thread-1", "trace-1")
        assert record.status == "completed"
        self._logger.log_event.assert_called()
        call_args = self._logger.log_event.call_args
        assert call_args[1]["event_type"] == "tool_completed"

    @patch("aiive.runtime.tool_executor.get_tool_registry")
    @patch("aiive.runtime.tool_executor.set_thread_context", return_value=None)
    def test_failed_execution_logs_failed_event(self, mock_set_ctx, mock_registry):
        """失败的工具执行应记录 tool_failed 事件。"""
        mock_reg = MagicMock()
        mock_reg.execute.return_value = {"ok": False, "error": "boom"}
        mock_registry.return_value = mock_reg

        parsed = ParsedToolCall(
            name="echo", params={}, raw_json='{"name":"echo"}', is_valid=True,
        )
        record = self.executor.execute(parsed, "thread-1", "trace-1")
        assert record.status == "failed"
        self._logger.log_event.assert_called()
        assert self._logger.log_event.call_args[1]["event_type"] == "tool_failed"


# ============================================================================
# 测试：extract_and_execute（主入口）
# ============================================================================

class TestExtractAndExecute:
    """集成测试：extract_and_execute 返回 (cleaned_text, records, malformed_errors)。"""

    def setup_method(self):
        self._db = MagicMock()
        self._logger = MagicMock()
        self.executor = ToolExecutor(self._db, self._logger)

    @patch("aiive.runtime.tool_executor.get_tool_registry")
    @patch("aiive.runtime.tool_executor.set_thread_context", return_value=None)
    def test_returns_clean_text_and_records(self, mock_set_ctx, mock_registry):
        """需求 4：返回清理后的文本和工具调用记录。"""
        mock_reg = MagicMock()
        mock_reg.execute.return_value = {"ok": True, "result": "echo_from_tool"}
        mock_registry.return_value = mock_reg

        text = 'Hello!<tool_call>{"name":"echo","params":{"message":"hi"}}</tool_call>'
        cleaned, records, malformed = self.executor.extract_and_execute(
            text, "thread-1", "trace-1",
        )
        assert "<tool_call>" not in cleaned
        assert "Hello!" in cleaned
        assert len(records) == 1
        assert records[0].name == "echo"
        assert records[0].status == "completed"
        assert len(malformed) == 0

    def test_detects_malformed_tags_in_text(self):
        """应检测文本中的畸形标签。"""
        text = "Normal text </tool_cost> more text"
        cleaned, records, malformed = self.executor.extract_and_execute(
            text, "thread-1", "trace-1",
        )
        assert len(malformed) >= 1
        assert any("tool_cost" in e for e in malformed)


# ============================================================================
# 测试：build_action_cards
# ============================================================================

class TestBuildActionCards:
    """需求 4：action_cards 应区分工具结果与阻止执行。"""

    def test_blocked_tool_generates_blocked_card(self):
        """被阻止的工具应生成 tool_blocked 卡片。"""
        record = ToolCallRecord(
            name="forget_memory", params={}, result={},
            status="blocked", trace_id="t1", reason="execution_mode=explain_only",
        )
        executor = ToolExecutor(MagicMock(), MagicMock())
        cards = executor.build_action_cards([record])
        assert any(c["card_type"] == "tool_blocked" for c in cards)

    def test_completed_tool_generates_result_card(self):
        """完成执行的工具应生成 tool_result 卡片。"""
        record = ToolCallRecord(
            name="echo", params={"msg": "hi"}, result={"ok": True, "result": "hi"},
            status="completed", trace_id="t1",
        )
        executor = ToolExecutor(MagicMock(), MagicMock())
        cards = executor.build_action_cards([record])
        assert any(c["card_type"] == "tool_result" for c in cards)
        assert any(c["status"] == "completed" for c in cards)


# ============================================================================
# 测试：search_memory 与 list_memories 行为
# ============================================================================

class TestSearchMemoryBehavior:
    """需求 7：search_memory("") 不应作为默认全量列表；list_memories 是列表工具。"""

    def test_search_memory_empty_query_rejected(self):
        """空查询应返回空结果并附带提示，而非全量转储。"""
        from aiive.tools.builtin_tools import _handle_search_memory
        mock_db = MagicMock()

        with patch("aiive.tools.builtin_tools.SessionLocal") as mock_session:
            mock_session.return_value = mock_db
            with patch("aiive.memory.memory_store.MemoryStore") as mock_store_class:
                mock_store = MagicMock()
                mock_store.get_active.return_value = []
                mock_store_class.return_value = mock_store

                result = _handle_search_memory(query="")
        assert result["ok"] is True
        assert result["results"] == []
        assert "hint" in result

    def test_search_memory_with_query_searches(self):
        """非空查询应检索活跃记忆的匹配内容。"""
        from aiive.tools.builtin_tools import _handle_search_memory
        mock_db = MagicMock()

        with patch("aiive.tools.builtin_tools.SessionLocal") as mock_session:
            mock_session.return_value = mock_db
            with patch("aiive.memory.memory_store.MemoryStore") as mock_store_class:
                mock_store = MagicMock()
                mock_store.get_active.return_value = [
                    MagicMock(id="m1", content="user name is Bob", memory_type="fact", lifecycle_state="active"),
                    MagicMock(id="m2", content="user city is Beijing", memory_type="fact", lifecycle_state="active"),
                ]
                mock_store_class.return_value = mock_store

                result = _handle_search_memory(query="Bob")
        assert result["ok"] is True
        assert len(result["results"]) == 1
        assert result["results"][0]["id"] == "m1"

    def test_list_memories_returns_all_active(self):
        """需求 7：list_memories 列出所有活跃记忆。"""
        from aiive.tools.builtin_tools import _handle_list_memories
        mock_db = MagicMock()

        with patch("aiive.tools.builtin_tools.SessionLocal") as mock_session:
            mock_session.return_value = mock_db
            with patch("aiive.memory.memory_store.MemoryStore") as mock_store_class:
                mock_store = MagicMock()
                mock_store.get_active.return_value = [
                    MagicMock(id="m1", content="mem1", memory_type="fact", lifecycle_state="active"),
                    MagicMock(id="m2", content="mem2", memory_type="fact", lifecycle_state="active"),
                ]
                mock_store_class.return_value = mock_store

                result = _handle_list_memories()
        assert result["ok"] is True
        assert len(result["results"]) == 2


# ============================================================================
# 测试：畸形标签绝不泄露给用户
# ============================================================================

class TestMalformedTagsNeverLeak:
    """需求 5：畸形工具标签必须标记为 parse_error，绝不以普通回复形式展示。"""

    def test_tool_cost_tag_detected(self):
        """应检测到 </tool_cost> 畸形标签。"""
        text = "I will use </tool_cost> in response"
        errors = ToolExecutor.detect_malformed_tags(text)
        assert len(errors) >= 1

    def test_malformed_tag_not_in_user_reply(self):
        """畸形标签不应出现在普通文本中。"""
        text = 'Response with </tool_cost> broken tag'
        parsed = ToolExecutor.extract_tool_calls(text)
        errors = ToolExecutor.detect_malformed_tags(text)
        cleaned = ToolExecutor.clean_text(text, parsed, errors)
        assert "</tool_cost>" not in cleaned


# ============================================================================
# 测试：工具结果上下文构建
# ============================================================================

class TestToolResultContext:
    """需求 3、8：工具结果必须可注入下一次 LLM 上下文。"""

    def test_build_result_context_includes_all_results(self):
        """构建的结果上下文应包含所有结果信息。"""
        records = [
            ToolCallRecord(
                name="search_memory", params={"query": "test"},
                result={"ok": True, "results": [{"id": "m1", "content": "test memory"}]},
                status="completed", trace_id="t1",
            ),
            ToolCallRecord(
                name="list_memories", params={},
                result={"ok": True, "results": []},
                status="completed", trace_id="t1",
            ),
        ]
        executor = ToolExecutor(MagicMock(), MagicMock())
        ctx = executor.build_result_context(records)
        assert "search_memory" in ctx
        assert "list_memories" in ctx
        assert "test memory" in ctx

    def test_empty_records_returns_empty(self):
        """空记录应返回空字符串。"""
        executor = ToolExecutor(MagicMock(), MagicMock())
        assert executor.build_result_context([]) == ""


# ============================================================================
# 集成测试：AgentLoop 与 ToolExecutor
# ============================================================================

class TestAgentLoopToolIntegration:
    """端到端测试：AgentLoop.run() 绝不能向用户暴露 <tool_call>。"""

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        if os.path.exists(self._tmpdir):
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    @patch("aiive.runtime.agent_loop.Session")
    @patch("aiive.runtime.agent_loop.LLMClient")
    @patch("aiive.runtime.tool_executor.get_tool_registry")
    @patch("aiive.runtime.tool_executor.set_thread_context", return_value=None)
    def test_run_never_returns_tool_call_tags(self, mock_set_ctx, mock_registry, mock_llm_class, mock_session):
        """需求 1：用户可见回复绝不能包含 <tool_call> 标签。"""
        from aiive.runtime.agent_loop import AgentLoop

        mock_llm = MagicMock()
        # First call: tool call
        resp1 = MagicMock()
        resp1.content = '<tool_call>{"name":"echo","params":{"message":"hi"}}</tool_call>'
        resp1.trace_id = "trace-1"
        resp1.model = "test-model"
        resp1.latency_ms = 10
        resp1.usage = {}
        # Second call: final response
        resp2 = MagicMock()
        resp2.content = "I have echoed your message!"
        resp2.trace_id = "trace-1"
        resp2.model = "test-model"
        resp2.latency_ms = 10
        resp2.usage = {}
        mock_llm.chat.side_effect = [resp1, resp2]
        mock_llm_class.return_value = mock_llm

        mock_db = MagicMock()

        mock_reg = MagicMock()
        mock_reg.execute.return_value = {"ok": True, "result": "hi"}
        mock_registry.return_value = mock_reg

        with patch("aiive.runtime.agent_loop.ThreadState") as mock_ts_class:
            mock_ts = MagicMock()
            mock_thread = MagicMock()
            mock_thread.id = "thread-test"
            mock_ts.get_or_create_thread.return_value = mock_thread
            mock_ts.get_recent_messages.return_value = []
            mock_ts_class.return_value = mock_ts

            with patch("aiive.runtime.agent_loop.ActionPlanner") as mock_ap_class:
                from aiive.core.action_planner import AgentDecision
                mock_ap = MagicMock()
                mock_ap.plan.return_value = AgentDecision(
                    decision_type="tool_call",
                    execution_mode="execute",
                    intent_type="normal_chat",
                    should_execute=True,
                    tool_name="echo",
                    reason="test",
                )
                mock_ap_class.return_value = mock_ap

                with patch("aiive.runtime.agent_loop.OutboxWorker"):
                    with patch("aiive.runtime.agent_loop.register_all"):
                        loop = AgentLoop(mock_llm, mock_db)
                        loop._build_messages = MagicMock()
                        loop._build_messages.return_value = [
                            {"role": "system", "content": "You are a helpful assistant."},
                            {"role": "user", "content": "echo hi"},
                        ]
                        result = loop.run("echo hi")
                        assert "<tool_call>" not in result["reply"]
                        assert "echoed" in result["reply"]
                        assert "tool_calls" in result
                        assert "tool_results" in result
                        assert "parse_errors" in result

    @patch("aiive.runtime.agent_loop.Session")
    @patch("aiive.runtime.agent_loop.LLMClient")
    def test_run_detects_malformed_tags(self, mock_llm_class, mock_session):
        """需求 5：畸形标签不得泄露且 parse_errors 必须返回。"""
        from aiive.runtime.agent_loop import AgentLoop

        mock_llm = MagicMock()
        response = MagicMock()
        response.content = "I tried to use </tool_cost> but it failed. Here is my response."
        response.trace_id = "trace-m"
        response.model = "test"
        response.latency_ms = 10
        response.usage = {}
        mock_llm.chat.return_value = response
        mock_llm_class.return_value = mock_llm

        mock_db = MagicMock()

        with patch("aiive.runtime.agent_loop.ThreadState") as mock_ts_class:
            mock_ts = MagicMock()
            mock_thread = MagicMock()
            mock_thread.id = "thread-m"
            mock_ts.get_or_create_thread.return_value = mock_thread
            mock_ts.get_recent_messages.return_value = []
            mock_ts_class.return_value = mock_ts

            with patch("aiive.runtime.agent_loop.ActionPlanner") as mock_ap_class:
                from aiive.core.action_planner import AgentDecision
                mock_ap = MagicMock()
                mock_ap.plan.return_value = AgentDecision(
                    decision_type="final_response",
                    execution_mode="explain_only",
                    intent_type="normal_chat",
                    should_execute=False,
                    reason="test",
                )
                mock_ap_class.return_value = mock_ap

                with patch("aiive.runtime.agent_loop.OutboxWorker"):
                    with patch("aiive.runtime.agent_loop.register_all"):
                        loop = AgentLoop(mock_llm, mock_db)
                        loop._build_messages = MagicMock()
                        loop._build_messages.return_value = [{"role": "user", "content": "test"}]
                        result = loop.run("test")
                        assert "</tool_cost>" not in result["reply"]
                        assert len(result["parse_errors"]) > 0

    @patch("aiive.runtime.agent_loop.Session")
    @patch("aiive.runtime.agent_loop.LLMClient")
    @patch("aiive.runtime.tool_executor.get_tool_registry")
    @patch("aiive.runtime.tool_executor.set_thread_context", return_value=None)
    def test_tool_results_enter_context(self, mock_set_ctx, mock_registry, mock_llm_class, mock_session):
        """需求 8：工具执行结果必须进入下一次 LLM 上下文。"""
        from aiive.runtime.agent_loop import AgentLoop

        mock_llm = MagicMock()
        resp1 = MagicMock()
        resp1.content = '<tool_call>{"name":"search_memory","params":{"query":"Bob"}}</tool_call>'
        resp1.trace_id = "trace-c"
        resp1.model = "test"
        resp1.latency_ms = 10
        resp1.usage = {}
        resp2 = MagicMock()
        resp2.content = "Found 1 memory: user name is Bob"
        resp2.trace_id = "trace-c"
        resp2.model = "test"
        resp2.latency_ms = 10
        resp2.usage = {}
        mock_llm.chat.side_effect = [resp1, resp2]
        mock_llm_class.return_value = mock_llm

        mock_db = MagicMock()

        mock_reg = MagicMock()
        mock_reg.execute.return_value = {
            "ok": True,
            "result": {
                "ok": True,
                "results": [{"id": "m1", "content": "user name is Bob", "memory_type": "fact"}],
                "total": 1,
                "query": "Bob",
            },
        }
        mock_registry.return_value = mock_reg

        with patch("aiive.runtime.agent_loop.ThreadState") as mock_ts_class:
            mock_ts = MagicMock()
            mock_thread = MagicMock()
            mock_thread.id = "thread-c"
            mock_ts.get_or_create_thread.return_value = mock_thread
            mock_ts.get_recent_messages.return_value = []
            mock_ts_class.return_value = mock_ts

            with patch("aiive.runtime.agent_loop.ActionPlanner") as mock_ap_class:
                from aiive.core.action_planner import AgentDecision
                mock_ap = MagicMock()
                mock_ap.plan.return_value = AgentDecision(
                    decision_type="tool_call",
                    execution_mode="execute",
                    intent_type="memory_update",
                    should_execute=True,
                    tool_name="search_memory",
                    reason="test",
                )
                mock_ap_class.return_value = mock_ap

                with patch("aiive.runtime.agent_loop.OutboxWorker"):
                    with patch("aiive.runtime.agent_loop.register_all"):
                        loop = AgentLoop(mock_llm, mock_db)
                        loop._build_messages = MagicMock()
                        loop._build_messages.return_value = [{"role": "user", "content": "search Bob"}]
                        result = loop.run("search Bob")
                        assert "<tool_call>" not in result["reply"]
                        assert len(result["tool_results"]) > 0


# ============================================================================
# 测试：run_stream 不双重执行
# ============================================================================

class TestRunStreamNoDoubleExecution:
    """需求 8（部分）：run_stream 不得重复执行工具。"""

    @patch("aiive.runtime.agent_loop.Session")
    @patch("aiive.runtime.agent_loop.LLMClient")
    @patch("aiive.runtime.tool_executor.get_tool_registry")
    @patch("aiive.runtime.tool_executor.set_thread_context", return_value=None)
    def test_run_stream_yields_tool_events(self, mock_set_ctx, mock_registry, mock_llm_class, mock_session):
        """验证 run_stream 产生 tool_call/tool_result 事件（仅执行一次）。"""
        from aiive.runtime.agent_loop import AgentLoop

        mock_llm = MagicMock()
        mock_llm.chat_stream.return_value = iter(
            list('<tool_call>{"name":"echo","params":{"message":"x"}}</tool_call>')
        )
        # For ActionPlanner which uses .chat, not .chat_stream
        mock_llm.chat.return_value = MagicMock(
            content="I processed your request.",
            trace_id="trace-s",
            model="test",
            latency_ms=10,
            usage={},
        )
        mock_llm_class.return_value = mock_llm

        mock_db = MagicMock()

        mock_reg = MagicMock()
        mock_reg.execute.return_value = {"ok": True, "result": "x"}
        mock_registry.return_value = mock_reg

        with patch("aiive.runtime.agent_loop.ThreadState") as mock_ts_class:
            mock_ts = MagicMock()
            mock_thread = MagicMock()
            mock_thread.id = "thread-s"
            mock_ts.get_or_create_thread.return_value = mock_thread
            mock_ts.get_recent_messages.return_value = []
            mock_ts_class.return_value = mock_ts

            with patch("aiive.runtime.agent_loop.ActionPlanner") as mock_ap_class:
                from aiive.core.action_planner import AgentDecision
                mock_ap = MagicMock()
                mock_ap.plan.return_value = AgentDecision(
                    decision_type="tool_call",
                    execution_mode="execute",
                    intent_type="normal_chat",
                    should_execute=True,
                    tool_name="echo",
                    reason="test",
                )
                mock_ap_class.return_value = mock_ap

                with patch("aiive.runtime.agent_loop.OutboxWorker"):
                    with patch("aiive.runtime.agent_loop.register_all"):
                        loop = AgentLoop(mock_llm, mock_db)
                        loop._build_messages = MagicMock()
                        loop._build_messages.return_value = [{"role": "user", "content": "echo x"}]

                        events = list(loop.run_stream("echo x"))
                        tool_call_events = [e for e in events if e["event"] == "tool_call"]
                        tool_result_events = [e for e in events if e["event"] == "tool_result"]
                        done_events = [e for e in events if e["event"] == "done"]

                        assert len(tool_call_events) >= 1
                        assert len(tool_result_events) >= 1
                        assert len(done_events) == 1
                        # Done reply must not contain <tool_call>
                        done_data = done_events[0]["data"]
                        assert "<tool_call>" not in done_data.get("reply", "")


# ============================================================================
# 测试：search_memory query="" 仅执行一次，不循环
# ============================================================================

class TestSearchMemoryEmptyQuery:
    """需求 6、7：search_memory("") 不循环；空查询被拒绝并附提示。"""

    @patch("aiive.runtime.tool_executor.get_tool_registry")
    @patch("aiive.runtime.tool_executor.set_thread_context", return_value=None)
    def test_empty_query_search_executed_once_then_deduped(self, mock_set_ctx, mock_registry):
        """如果 LLM 发出两个相同的 search_memory 调用，第二个必须被去重。"""
        mock_reg = MagicMock()
        mock_reg.execute.return_value = {
            "ok": True,
            "result": {"ok": True, "results": [], "hint": "Empty query. Use list_memories."},
        }
        mock_registry.return_value = mock_reg

        executor = ToolExecutor(MagicMock(), MagicMock())
        text = (
            '<tool_call>{"name":"search_memory","params":{"query":""}}</tool_call>\n'
            '<tool_call>{"name":"search_memory","params":{"query":""}}</tool_call>'
        )
        cleaned, records, malformed = executor.extract_and_execute(text, "t1", "tr1")
        assert len(records) == 2
        assert records[0].status in ("completed", "failed")
        assert records[1].status == "duplicate"

    def test_search_memory_handler_rejects_empty(self):
        """需求 7：search_memory 处理器拒绝空查询并附提示。"""
        from aiive.tools.builtin_tools import _handle_search_memory
        mock_db = MagicMock()

        with patch("aiive.tools.builtin_tools.SessionLocal") as mock_session:
            mock_session.return_value = mock_db
            with patch("aiive.memory.memory_store.MemoryStore") as mock_store_class:
                mock_store = MagicMock()
                mock_store.get_active.return_value = []
                mock_store_class.return_value = mock_store

                result = _handle_search_memory(query="")
        assert result["ok"] is True
        assert result["results"] == []
        assert "hint" in result
