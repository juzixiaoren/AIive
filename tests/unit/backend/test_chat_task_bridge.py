"""测试 Chat 到 Task 桥接层——工具注册。"""


class TestChatTaskBridge:
    def test_tool_registry_has_schedule_reminder(self):
        """验证工具注册表中存在 schedule_reminder 工具。"""
        from aiive.tools.registry import get_tool_registry
        registry = get_tool_registry()
        reg = registry.get("schedule_reminder")
        assert reg is not None
        assert reg.safety.risk_level == "low"

    def test_schedule_reminder_requires_execution_identity(self):
        """副作用工具缺少持久化执行身份时必须被拒绝（新安全契约）。

        生产环境：writes_external_world=True 的工具必须经操作执行器 +
        审批流程执行，run_context 需携带 turn_record_id 与 tool_call_id，
        否则 registry.execute 直接拒绝，避免在测试/未授权路径下同步写库。
        """
        from aiive.tools.registry import get_tool_registry
        registry = get_tool_registry()
        from aiive.context.run_context import RunContext

        result = registry.execute(
            "schedule_reminder",
            {"content": "test-reminder", "delay_minutes": 60},
            "trusted_user_command",
            run_context=RunContext(thread_id="t-x", trace_id="test-trace", source="test"),
        )
        assert result["ok"] is False
        assert result.get("error_type") == "missing_execution_identity"
