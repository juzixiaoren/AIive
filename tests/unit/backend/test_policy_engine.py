"""测试 PolicyEngine（策略引擎）的批次判定逻辑。

回归覆盖 P0 问题 3：被阻止（含未知）工具混入批次时，
无论同批是否还有允许/确认工具，都必须阻止整批次。
"""

from aiive.runtime.policy_engine import PolicyAction, check_tool_calls
from aiive.tools.registry import CapabilitySafetySchema, ToolRegistration, ToolRegistry


def _register(registry: ToolRegistry, name: str, **safety_kwargs) -> None:
    safety_kwargs.setdefault("risk_level", "low")
    schema = CapabilitySafetySchema(
        capability_id=name,
        definition_source="local_builtin",
        definition_trust_level="trusted",
        **safety_kwargs,
    )
    registry.register(ToolRegistration(safety=schema, handler=lambda **kwargs: "ok"))


class TestPolicyBatchBlocking:
    """被阻止工具混入批次时必须阻止整批次。"""

    def test_unknown_tool_alone_blocked(self):
        """未知工具应被阻止。"""
        registry = ToolRegistry()
        _register(registry, "known")
        result = check_tool_calls(
            [{"name": "unknown_tool", "args": {}, "id": "1"}],
            registry=registry,
        )
        assert result.action == PolicyAction.BLOCK
        assert "unknown_tool" in result.blocked_tools

    def test_blocked_and_allowed_blocked(self):
        """批次同时含未知工具与正常工具时，整批次必须阻止。"""
        registry = ToolRegistry()
        _register(registry, "known")
        result = check_tool_calls(
            [
                {"name": "known", "args": {}, "id": "1"},
                {"name": "unknown_tool", "args": {}, "id": "2"},
            ],
            registry=registry,
        )
        assert result.action == PolicyAction.BLOCK
        assert "unknown_tool" in result.blocked_tools

    def test_blocked_and_confirm_blocked(self):
        """批次同时含未知工具与需确认工具时，整批次必须阻止。"""
        registry = ToolRegistry()
        _register(registry, "known")
        _register(registry, "danger", risk_level="high")
        result = check_tool_calls(
            [
                {"name": "danger", "args": {}, "id": "1"},
                {"name": "unknown_tool", "args": {}, "id": "2"},
            ],
            registry=registry,
        )
        assert result.action == PolicyAction.BLOCK
        assert "unknown_tool" in result.blocked_tools

    def test_allowed_and_confirm_blocked(self):
        """批次同时含允许与需确认工具时，为安全起见阻止整批次。"""
        registry = ToolRegistry()
        _register(registry, "known")
        _register(registry, "danger", risk_level="high")
        result = check_tool_calls(
            [
                {"name": "known", "args": {}, "id": "1"},
                {"name": "danger", "args": {}, "id": "2"},
            ],
            registry=registry,
        )
        assert result.action == PolicyAction.BLOCK

    def test_only_confirm_allowed(self):
        """仅含确认工具（无允许、无阻止）时返回 CONFIRM。"""
        registry = ToolRegistry()
        _register(registry, "danger", risk_level="high")
        result = check_tool_calls(
            [{"name": "danger", "args": {}, "id": "1"}],
            registry=registry,
        )
        assert result.action == PolicyAction.CONFIRM
        assert "danger" in result.confirm_tools

    def test_only_allowed_allowed(self):
        """仅含允许工具时返回 ALLOW。"""
        registry = ToolRegistry()
        _register(registry, "known")
        result = check_tool_calls(
            [{"name": "known", "args": {}, "id": "1"}],
            registry=registry,
        )
        assert result.action == PolicyAction.ALLOW
