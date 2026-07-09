"""测试 PermissionManager（权限管理器）模块。

验证工具的安全权限检查，包括信任来源、不可信来源拦截、确认要求等。
"""

from aiive.tools.permission_manager import PermissionManager
from aiive.tools.registry import (
    CapabilitySafetySchema,
    ToolRegistration,
    ToolRegistry,
)


def _make_registry_with_tools() -> ToolRegistry:
    """创建包含安全工具和风险工具的注册表，用于权限测试。"""
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
    """测试 PermissionManager 的权限检查逻辑。"""

    def setup_method(self):
        registry = _make_registry_with_tools()
        self.pm = PermissionManager(registry)

    def test_trusted_source_can_trigger_safe_tool(self):
        """可信来源应能触发安全工具。"""
        assert self.pm.can_trigger_from("safe_tool", "trusted_user_command") is True

    def test_untrusted_source_cannot_trigger(self):
        """不可信来源不能触发任何工具。"""
        assert self.pm.can_trigger_from("safe_tool", "untrusted_external_content") is False

    def test_unknown_tool_denied(self):
        """未知工具应被拒绝。"""
        assert self.pm.can_trigger_from("nonexistent", "trusted_user_command") is False

    def test_check_with_confirmation(self):
        """风险工具应要求确认。"""
        result = self.pm.check("risky_tool", "trusted_user_command")
        assert result["allowed"] is False
        assert result["requires_confirmation"] is True

    def test_check_safe_tool(self):
        """安全工具应允许执行。"""
        result = self.pm.check("safe_tool", "trusted_user_command")
        assert result["allowed"] is True

    def test_is_safe_for_untrusted_content(self):
        """对不可信内容的安全检查：安全工具为 True，风险工具为 False。"""
        assert self.pm.is_safe_for_untrusted_content("safe_tool") is True
        assert self.pm.is_safe_for_untrusted_content("risky_tool") is False

    def test_unknown_not_safe_for_untrusted(self):
        """未知工具对不可信内容应返回不安全。"""
        assert self.pm.is_safe_for_untrusted_content("unknown") is False
