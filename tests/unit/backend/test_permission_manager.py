from aiive.tools.permission_manager import PermissionManager
from aiive.tools.registry import (
    CapabilitySafetySchema,
    ToolRegistration,
    ToolRegistry,
)


def _make_registry_with_tools() -> ToolRegistry:
    registry = ToolRegistry()

    def handler(**kw):
        return "ok"

    registry.register(ToolRegistration(
        safety=CapabilitySafetySchema("safe_tool", "local_builtin", "trusted", "low"),
        handler=handler,
    ))
    registry.register(ToolRegistration(
        safety=CapabilitySafetySchema(
            "risky_tool", "local_builtin", "trusted", "medium",
            requires_confirmation=True, writes_external_world=True,
        ),
        handler=handler,
    ))
    return registry


class TestPermissionManager:
    def setup_method(self):
        registry = _make_registry_with_tools()
        self.pm = PermissionManager(registry)

    def test_trusted_source_can_trigger_safe_tool(self):
        assert self.pm.can_trigger_from("safe_tool", "trusted_user_command") is True

    def test_untrusted_source_cannot_trigger(self):
        assert self.pm.can_trigger_from("safe_tool", "untrusted_external_content") is False

    def test_unknown_tool_denied(self):
        assert self.pm.can_trigger_from("nonexistent", "trusted_user_command") is False

    def test_check_with_confirmation(self):
        result = self.pm.check("risky_tool", "trusted_user_command")
        assert result["allowed"] is False
        assert result["requires_confirmation"] is True

    def test_check_safe_tool(self):
        result = self.pm.check("safe_tool", "trusted_user_command")
        assert result["allowed"] is True

    def test_is_safe_for_untrusted_content(self):
        assert self.pm.is_safe_for_untrusted_content("safe_tool") is True
        assert self.pm.is_safe_for_untrusted_content("risky_tool") is False

    def test_unknown_not_safe_for_untrusted(self):
        assert self.pm.is_safe_for_untrusted_content("unknown") is False
