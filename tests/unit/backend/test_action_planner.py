"""记忆提取信号与提取策略测试。"""

import pytest

from aiive.core.action_planner import ActionPlanner
from aiive.core.llm_client import FakeLLMClient
from aiive.memory.extraction_policy import MemoryExtractionPolicy, MemorySignalAction


class TestClassifyMemorySignal:
    """验证 classify_memory_signal 的 prompt 渲染不再因裸花括号抛 KeyError。"""

    def test_prompt_rendering_does_not_raise_keyerror(self):
        """含字面量 JSON 示例的模板不应因 str.format 而失败。"""
        fake = FakeLLMClient(fixed_content='{"action": "skip", "confidence": 0.9, "reason": "greeting"}')
        planner = ActionPlanner(fake)

        signal = planner.classify_memory_signal(
            user_message="你好", reply="你好，有什么可以帮你？", trace_id="t1",
        )

        assert signal.action == MemorySignalAction.SKIP.value
        assert signal.confidence == 0.9

    def test_prompt_contains_rendered_message_and_json_example(self):
        """渲染后的 prompt 应替换占位符，且保留 JSON 示例中的花括号。"""
        fake = FakeLLMClient(fixed_content='{"action": "extract_async", "confidence": 0.5, "reason": ""}')
        planner = ActionPlanner(fake)

        planner.classify_memory_signal(
            user_message="我叫小明", reply="好的，小明。", trace_id="t2",
        )

        sent_prompt = fake._call_history[0]["messages"][0]["content"]
        assert "我叫小明" in sent_prompt
        assert "好的，小明。" in sent_prompt
        assert "{user_message}" not in sent_prompt
        assert "{reply}" not in sent_prompt
        # JSON 示例中的字面量花括号应原样保留
        assert '"action": "skip" | "extract_async" | "extract_sync"' in sent_prompt

    def test_malformed_json_repaired(self):
        """模型返回带尾逗号的非法 JSON 时应经 repair_json 兜底解析成功。"""
        fake = FakeLLMClient(fixed_content='{"action": "extract_sync", "confidence": 0.8, "reason": "rule",}')
        planner = ActionPlanner(fake)

        signal = planner.classify_memory_signal(
            user_message="以后叫我老板", reply="好的。", trace_id="t3",
        )

        assert signal.action == MemorySignalAction.EXTRACT_SYNC.value
        assert signal.confidence == 0.8


class TestMemoryExtractionPolicy:
    """验证模型动作与确定性结构守卫的合并规则。"""

    @pytest.mark.parametrize(
        "message",
        ["", "   ", "[system command] 执行提醒", "[runtime event] task_due", "[系统指令] 确认提醒"],
    )
    def test_system_messages_are_skipped(self, message: str):
        action = MemoryExtractionPolicy.resolve_action(
            MemorySignalAction.EXTRACT_SYNC,
            message,
        )

        assert action == MemorySignalAction.SKIP

    @pytest.mark.parametrize("action", list(MemorySignalAction))
    def test_valid_model_action_is_preserved(self, action: MemorySignalAction):
        resolved = MemoryExtractionPolicy.resolve_action(action.value, "普通用户消息")

        assert resolved == action

    @pytest.mark.parametrize("action", ["unknown", "", None])
    def test_invalid_action_falls_back_to_async(self, action: str | None):
        resolved = MemoryExtractionPolicy.resolve_action(action, "普通用户消息")  # type: ignore[arg-type]

        assert resolved == MemorySignalAction.EXTRACT_ASYNC
