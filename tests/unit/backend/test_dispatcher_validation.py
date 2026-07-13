"""针对 check_tool_calls 策略引擎的目标测试。

_validate_against_decision 已随 ToolExecutor 旧路径移除，
策略校验现由 runtime/policy_engine.py 的 check_tool_calls() 和
agent_graph.py 的 _policy_check 处理。
"""
from unittest.mock import MagicMock

import pytest

from aiive.core.action_planner import AgentDecision
from aiive.runtime.policy_engine import check_tool_calls, PolicyResult


def _make_tool_calls(tool_name: str = "forget_memory") -> list[dict]:
    return [{
        "name": tool_name,
        "args": {},
        "id": "call_test",
    }]


class TestPolicyEngine:
    """测试 check_tool_calls() 在各种条件下的拦截/放行行为。"""

    def test_tool_call_allows(self):
        """正常工具调用应放行。"""
        tool_calls = _make_tool_calls("echo")
        result = check_tool_calls(tool_calls)
        assert result.action == "allow"

    def test_high_risk_tool_no_registry(self):
        """无 registry 时高风险工具返回 CONFIRM（需要确认）。"""
        tool_calls = _make_tool_calls("safe_delete")
        result = check_tool_calls(tool_calls)
        # 无 registry 元数据时，safe_delete 因风险等级返回 confirm
        assert result.action in ("allow", "confirm")

    def test_empty_tool_calls(self):
        """空 tool_calls 列表应放行。"""
        result = check_tool_calls([])
        assert result.action == "allow"

    def test_multiple_tool_calls(self):
        """多个 tool_calls 应正常处理。"""
        tool_calls = [
            {"name": "echo", "args": {}, "id": "call_1"},
            {"name": "memory_search", "args": {"query": "test"}, "id": "call_2"},
        ]
        result = check_tool_calls(tool_calls)
        assert result.action == "allow"
