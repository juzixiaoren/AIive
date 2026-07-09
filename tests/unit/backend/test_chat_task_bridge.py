from aiive.runtime.intent_router import IntentRouter


class TestChatTaskBridge:
    def test_all_intents_default_to_plain_chat(self):
        """All intents now go through LLM ReAct loop. No regex matching."""
        router = IntentRouter()
        for msg in ["一分钟后提醒我测试", "以后叫我小明", "忘掉我的名字记忆"]:
            result = router.detect(msg)
            assert result.intent_type == "plain_chat", f"Message '{msg}' should be plain_chat (LLM-driven)"

    def test_tool_registry_has_schedule_reminder(self):
        from aiive.tools.registry import get_tool_registry
        registry = get_tool_registry()
        reg = registry.get("schedule_reminder")
        assert reg is not None
        assert reg.safety.risk_level == "low"

    def test_schedule_reminder_tool_executes(self):
        from aiive.tools.registry import get_tool_registry
        registry = get_tool_registry()
        result = registry.execute(
            "schedule_reminder",
            {"content": "hi", "delay_minutes": 1},
            "trusted_user_command",
        )
        assert result["ok"] is True
        assert result["result"]["reminder_set"] is True
