"""测试 Chat 到 Task 桥接层——工具注册。"""


class TestChatTaskBridge:
    def test_tool_registry_has_schedule_reminder(self):
        """验证工具注册表中存在 schedule_reminder 工具。"""
        from aiive.tools.registry import get_tool_registry
        registry = get_tool_registry()
        reg = registry.get("schedule_reminder")
        assert reg is not None
        assert reg.safety.risk_level == "low"

    def test_schedule_reminder_tool_executes(self):
        """验证 schedule_reminder 工具可正常执行。"""
        from aiive.tools.registry import get_tool_registry
        registry = get_tool_registry()
        from aiive.context.run_context import RunContext
        from aiive.runtime.thread_bootstrap import ThreadBootstrapService

        # 确保测试线程已 committed，避免 FK 违规
        tid = ThreadBootstrapService.ensure_committed_thread(None)
        result = registry.execute(
            "schedule_reminder",
            {"content": "test-reminder", "delay_minutes": 60},
            "trusted_user_command",
            run_context=RunContext(thread_id=tid, trace_id="test-trace", source="test"),
        )
        assert result["ok"] is True
        assert result["result"]["reminder_set"] is True
