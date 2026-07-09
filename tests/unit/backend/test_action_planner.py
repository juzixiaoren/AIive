"""Tests for AgentDecision model. ActionPlanner.plan() is now simplified
(deferred to LLM native tool_calls in agent_graph)."""

import pytest

from aiive.core.action_planner import AgentDecision, ActionPlanner
from aiive.core.llm_client import FakeLLMClient


class TestAgentDecisionModel:
    def test_default_fields(self):
        d = AgentDecision()
        assert d.decision_type == "final_response"
        assert d.execution_mode == "explain_only"
        assert d.should_execute is False

    def test_to_intent_dict_maps_correctly(self):
        d = AgentDecision(
            decision_type="tool_call",
            execution_mode="execute",
            intent_type="memory_forget_request",
            should_execute=True,
            tool_name="forget_memory",
            tool_params={"scope": "all"},
            reason="User requested clearing memory",
        )
        rd = d.to_intent_dict()
        assert rd["intent_type"] == "memory_forget_request"
        assert rd["execution_mode"] == "execute"
        assert rd["should_execute"] is True
        assert rd["candidate_tool"] == "forget_memory"

    def test_validation_rejects_invalid_confidence(self):
        with pytest.raises(Exception):
            AgentDecision(
                decision_type="tool_call",
                execution_mode="execute",
                intent_type="command",
                should_execute=True,
                confidence=2.0,
                reason="bad",
            )


class TestActionPlanner:
    """ActionPlanner.plan() now returns default decisions — intent routing
    is handled by LLM native tool_calls in the agent graph."""

    def test_plan_returns_default_decision(self):
        llm = FakeLLMClient()
        planner = ActionPlanner(llm)
        decision = planner.plan(user_message="清空记忆")
        assert decision.decision_type == "final_response"
        assert decision.execution_mode == "explain_only"
        assert decision.should_execute is False
        assert decision.tool_name is None

    def test_plan_consistent_for_different_messages(self):
        llm = FakeLLMClient()
        planner = ActionPlanner(llm)

        d1 = planner.plan(user_message="记住我的名字")
        d2 = planner.plan(user_message="删除提醒")
        d3 = planner.plan(user_message="今天天气怎么样")

        # All should return consistent default
        for d in [d1, d2, d3]:
            assert d.decision_type == "final_response"
            assert d.execution_mode == "explain_only"

    def test_plan_to_intent_dict(self):
        llm = FakeLLMClient()
        planner = ActionPlanner(llm)
        decision = planner.plan(user_message="hello")
        intent = decision.to_intent_dict()
        assert "intent_type" in intent
        assert "execution_mode" in intent
