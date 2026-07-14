"""Tests for AgentDecision model."""

import pytest

from aiive.core.action_planner import AgentDecision


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
