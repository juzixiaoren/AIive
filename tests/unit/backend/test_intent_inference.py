"""Targeted tests for _infer_intent refactor.

Verifies the rule-based classifier returns correct execution_mode for:
- High-confidence execute commands (including forget/clear)
- High-confidence explain/hypothetical questions
- model_decide fallback for ambiguous input
- Polite requests ("能不能帮我X") → execute
- Hypothetical questions ("如果...你会怎么做") → explain_only
"""
from unittest.mock import MagicMock

import pytest

from aiive.runtime.agent_loop import AgentLoop


def _classify(message: str) -> dict:
    """Helper: instantiate AgentLoop with mocks and call _infer_intent."""
    loop = AgentLoop(llm_client=MagicMock(), db=MagicMock())
    return loop._infer_intent(message)


# ── Execute / forget ──

class TestClearForget:
    def test_clear_memory_is_execute(self):
        r = _classify("清空记忆")
        assert r["execution_mode"] == "execute"
        assert r["should_execute"] is True
        assert r["intent_type"] == "memory_forget_request"

    def test_clear_memory_with_context_is_execute(self):
        r = _classify("好的，现在开始清空记忆。先查询所有记忆条目。")
        assert r["execution_mode"] == "execute"
        assert r["should_execute"] is True
        assert r["intent_type"] == "memory_forget_request"

    def test_forget_name_is_execute(self):
        r = _classify("忘掉我刚才说的名字")
        assert r["execution_mode"] == "execute"
        assert r["should_execute"] is True
        assert r["intent_type"] == "memory_forget_request"

    def test_forget_english_is_execute(self):
        r = _classify("forget everything")
        assert r["execution_mode"] == "execute"
        assert r["should_execute"] is True

    def test_clear_english_is_execute(self):
        r = _classify("clear memory")
        assert r["execution_mode"] == "execute"
        assert r["should_execute"] is True


# ── Execute / general commands ──

class TestGeneralExecute:
    def test_remember_is_execute(self):
        r = _classify("记住我喜欢喝咖啡")
        assert r["execution_mode"] == "execute"
        assert r["should_execute"] is True
        assert r["intent_type"] == "memory_update"

    def test_my_name_is_execute(self):
        r = _classify("我叫小明")
        assert r["execution_mode"] == "execute"
        assert r["should_execute"] is True

    def test_reminder_is_execute(self):
        r = _classify("一分钟后提醒我 hi")
        assert r["execution_mode"] == "execute"
        assert r["should_execute"] is True
        assert r["intent_type"] == "reminder_create"

    def test_delete_is_execute(self):
        r = _classify("删除 test.txt")
        assert r["execution_mode"] == "execute"
        assert r["should_execute"] is True

    def test_bangwo_is_execute(self):
        r = _classify("帮我创建一个提醒")
        assert r["execution_mode"] == "execute"
        assert r["should_execute"] is True


# ── Polite requests → execute ──

class TestPoliteRequests:
    def test_can_you_help_remember_name_is_execute(self):
        r = _classify("能不能帮我记住我的名字叫B？")
        assert r["execution_mode"] == "execute"
        assert r["should_execute"] is True

    def test_can_you_help_set_preference_is_execute(self):
        r = _classify("能不能帮我设置每天早上8点提醒？")
        assert r["execution_mode"] == "execute"
        assert r["should_execute"] is True


# ── Explain / hypothetical → explain_only ──

class TestExplainOnly:
    def test_hypothetical_clear_is_explain(self):
        r = _classify("如果我想清空记忆，你会怎么做？")
        assert r["execution_mode"] == "explain_only"
        assert r["should_execute"] is False

    def test_hypothetical_remember_is_explain(self):
        r = _classify("如果我要你记住我的名字，你会怎么做")
        assert r["execution_mode"] == "explain_only"
        assert r["should_execute"] is False

    def test_what_tool_is_explain(self):
        r = _classify("你会调用哪个工具来删除文件？")
        assert r["execution_mode"] == "explain_only"
        assert r["should_execute"] is False

    def test_how_to_implement_is_explain(self):
        r = _classify("一分钟后提醒功能怎么实现？")
        assert r["execution_mode"] == "explain_only"
        assert r["should_execute"] is False


# ── Polite question (not request) → explain_only ──

class TestPoliteQuestions:
    def test_can_you_tell_how_is_explain(self):
        r = _classify("能不能告诉我你会怎么记住名字？")
        assert r["execution_mode"] == "explain_only"
        assert r["should_execute"] is False

    def test_can_you_explain_is_explain(self):
        r = _classify("能不能介绍一下记忆功能？")
        assert r["execution_mode"] == "explain_only"
        assert r["should_execute"] is False


# ── Normal chat → model_decide ──

class TestModelDecide:
    def test_code_error_is_model_decide(self):
        r = _classify("我的代码报错了")
        assert r["execution_mode"] == "model_decide"
        assert r["should_execute"] is False
        assert r["intent_type"] == "normal_chat"

    def test_tired_is_model_decide(self):
        r = _classify("今天好累")
        assert r["execution_mode"] == "model_decide"
        assert r["should_execute"] is False

    def test_greeting_is_model_decide(self):
        r = _classify("你好")
        assert r["execution_mode"] == "model_decide"
        assert r["should_execute"] is False

    def test_casual_chat_is_model_decide(self):
        r = _classify("最近有什么新闻吗")
        assert r["execution_mode"] == "model_decide"
        assert r["should_execute"] is False
