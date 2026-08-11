"""测试 PolicyEngine（策略引擎）的批次判定逻辑。

删除、高风险和显式确认工具进入审批；未知工具混入批次时阻止整批次。
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

    def test_unknown_and_high_risk_blocked(self):
        """高风险工具需确认，但同批未知工具仍优先使整批阻止。"""
        registry = ToolRegistry()
        _register(registry, "danger", risk_level="high")
        result = check_tool_calls(
            [
                {"name": "danger", "args": {}, "id": "1"},
                {"name": "unknown_tool", "args": {}, "id": "2"},
            ],
            registry=registry,
        )
        assert result.action == PolicyAction.BLOCK
        assert result.allowed_tools == ["danger"]
        assert result.blocked_tools == ["unknown_tool"]

    def test_registered_risk_metadata_requires_confirmation(self):
        """高风险、删除和显式确认元数据应进入审批，普通写入保持高权限直通。"""
        registry = ToolRegistry()
        _register(registry, "normal")
        _register(registry, "high_risk", risk_level="high")
        _register(registry, "destructive", can_delete=True)
        _register(registry, "external_write", risk_level="medium", writes_external_world=True)
        _register(registry, "confirmation_marked", requires_confirmation=True)
        tool_names = [
            "normal", "high_risk", "destructive", "external_write", "confirmation_marked",
        ]
        result = check_tool_calls(
            [
                {"name": name, "args": {}, "id": str(index)}
                for index, name in enumerate(tool_names)
            ],
            registry=registry,
        )
        assert result.action == PolicyAction.CONFIRM
        assert result.allowed_tools == tool_names
        assert result.confirm_tools == ["high_risk", "destructive", "confirmation_marked"]
        assert result.blocked_tools == []

    def test_only_registered_tool_allowed(self):
        """仅含普通已注册工具时返回 ALLOW。"""
        registry = ToolRegistry()
        _register(registry, "known")
        result = check_tool_calls(
            [{"name": "known", "args": {}, "id": "1"}],
            registry=registry,
        )
        assert result.action == PolicyAction.ALLOW
