"""针对 ToolExecutor._validate_against_decision() 的目标测试。"""

from unittest.mock import MagicMock

import pytest

from aiive.core.action_planner import AgentDecision
from aiive.runtime.tool_executor import ToolExecutor


def _validate(tool_name: str, decision: AgentDecision | None) -> tuple[bool, str]:
    """辅助函数：调用 ToolExecutor 的静态校验器。"""
    if decision is None:
        return ToolExecutor._validate_against_decision(
            tool_name,
            AgentDecision(
                decision_type="tool_call",
                execution_mode="execute",
                intent_type="normal_chat",
                should_execute=True,
                reason="null fallback",
            ),
        )
    return ToolExecutor._validate_against_decision(tool_name, decision)


class TestDispatcherValidation:
    """通过 ToolExecutor._validate_against_decision 测试 Dispatcher 机制规则。"""

    def test_null_decision_allows(self):
        """无决策 → 视为放行（使用宽松默认值校验）"""
        d = AgentDecision(
            decision_type="tool_call",
            execution_mode="execute",
            intent_type="normal_chat",
            should_execute=True,
            reason="fallback",
        )
        ok, _ = ToolExecutor._validate_against_decision("forget_memory", d)
        assert ok is True

    def test_final_response_blocks_tools(self):
        """Agent 判定为 final_response → 拦截所有工具"""
        d = AgentDecision(
            decision_type="final_response",
            execution_mode="explain_only",
            intent_type="normal_chat",
            should_execute=False,
            reason="hypothetical",
        )
        ok, reason = ToolExecutor._validate_against_decision("forget_memory", d)
        assert ok is False
        assert "final_response" in reason.lower()

    def test_ask_clarification_blocks_tools(self):
        """需要澄清 → 拦截工具"""
        d = AgentDecision(
            decision_type="ask_clarification",
            execution_mode="explain_only",
            intent_type="normal_chat",
            should_execute=False,
            reason="unclear",
        )
        ok, _ = ToolExecutor._validate_against_decision("remember_or_update", d)
        assert ok is False

    def test_explain_only_blocks_tools(self):
        """explain_only → 拦截会产生副作用的工具"""
        d = AgentDecision(
            decision_type="tool_call",
            execution_mode="explain_only",
            intent_type="normal_chat",
            should_execute=True,
            reason="should explain",
        )
        ok, reason = ToolExecutor._validate_against_decision("forget_memory", d)
        assert ok is False

    def test_dry_run_blocks_tools(self):
        """dry_run → 拦截工具"""
        d = AgentDecision(
            decision_type="tool_call",
            execution_mode="dry_run",
            intent_type="normal_chat",
            should_execute=True,
        )
        ok, _ = ToolExecutor._validate_against_decision("schedule_reminder", d)
        assert ok is False

    def test_should_execute_false_blocks(self):
        """should_execute=False → 拦截"""
        d = AgentDecision(
            decision_type="tool_call",
            execution_mode="execute",
            intent_type="memory_forget_request",
            should_execute=False,
        )
        ok, _ = ToolExecutor._validate_against_decision("forget_memory", d)
        assert ok is False

    def test_execute_allows(self):
        """tool_call + execute + should_execute=True → 放行"""
        d = AgentDecision(
            decision_type="tool_call",
            execution_mode="execute",
            intent_type="memory_forget_request",
            should_execute=True,
            tool_name="forget_memory",
            reason="user requested",
        )
        ok, _ = ToolExecutor._validate_against_decision("forget_memory", d)
        assert ok is True

    def test_execute_reminder_allows(self):
        """提醒类工具的 execute 模式应放行。"""
        d = AgentDecision(
            decision_type="tool_call",
            execution_mode="execute",
            intent_type="reminder_create",
            should_execute=True,
            tool_name="schedule_reminder",
        )
        ok, _ = ToolExecutor._validate_against_decision("schedule_reminder", d)
        assert ok is True

    def test_execute_memory_write_allows(self):
        """记忆写入类工具的 execute 模式应放行。"""
        d = AgentDecision(
            decision_type="tool_call",
            execution_mode="execute",
            intent_type="user_identity_update",
            should_execute=True,
            tool_name="remember_or_update",
        )
        ok, _ = ToolExecutor._validate_against_decision("remember_or_update", d)
        assert ok is True
