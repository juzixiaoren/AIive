"""测试 UnifiedMemoryExtractor 信号提取能力。

验证从用户对话中提取例行任务、偏好、用户资料等个人信号的功能。
（原 StewardSignalExtractor 别名已移除，测试直接使用 UnifiedMemoryExtractor，
返回值为 MemoryProposal 对象而非 dict。）
"""

import json

from aiive.core.llm_client import FakeLLMClient
from aiive.memory.memory_extractor import UnifiedMemoryExtractor
from aiive.memory.memory_types import MemoryProposal


class TestStewardSignalExtractor:
    """测试 UnifiedMemoryExtractor 的信号提取功能。"""

    def test_extract_routine_signal(self):
        """应正确提取例行任务信号。"""
        fake = FakeLLMClient(
            fixed_content=json.dumps([
                {
                    "signal_type": "routine",
                    "content": "User goes to gym every morning at 7am",
                    "confidence": 0.9,
                    "schedule_text": "daily at 7am",
                }
            ]),
        )
        extractor = UnifiedMemoryExtractor(fake)
        results = extractor.extract("我每天早上7点去健身房", "好的")
        assert len(results) == 1
        assert isinstance(results[0], MemoryProposal)
        assert results[0].content == "User goes to gym every morning at 7am"

    def test_extract_preference_signal(self):
        """应正确提取偏好信号。"""
        fake = FakeLLMClient(
            fixed_content=json.dumps([
                {
                    "signal_type": "preference",
                    "content": "User prefers working in dark mode",
                    "confidence": 0.8,
                }
            ]),
        )
        extractor = UnifiedMemoryExtractor(fake)
        results = extractor.extract("我喜欢暗色模式", "了解了")
        assert len(results) == 1
        assert isinstance(results[0], MemoryProposal)
        assert "dark mode" in results[0].content

    def test_extract_profile_signal(self):
        """应正确提取用户资料信号。"""
        fake = FakeLLMClient(
            fixed_content=json.dumps([
                {
                    "signal_type": "user_profile",
                    "content": "User is a software engineer in Beijing",
                    "confidence": 0.95,
                }
            ]),
        )
        extractor = UnifiedMemoryExtractor(fake)
        results = extractor.extract("我是北京的软件工程师", "收到")
        assert len(results) == 1
        assert isinstance(results[0], MemoryProposal)

    def test_filters_invalid_signal_types(self):
        """所有 raw entry 都经 ProposalNormalizer 规范化后返回，不会丢失。"""
        fake = FakeLLMClient(
            fixed_content=json.dumps([
                {"signal_type": "routine", "content": "valid", "confidence": 0.9},
                {"signal_type": "unknown", "content": "bad", "confidence": 0.1},
            ])
        )
        extractor = UnifiedMemoryExtractor(fake)
        results = extractor.extract("test", "ok")
        # UnifiedMemoryExtractor normalizes all entries to MemoryProposal;
        # no signal_type filtering at extract level (normalization handles it).
        assert len(results) == 2
        assert all(isinstance(r, MemoryProposal) for r in results)

    def test_handles_invalid_json(self):
        """无效 JSON 应返回空列表。"""
        fake = FakeLLMClient(fixed_content="not json")
        extractor = UnifiedMemoryExtractor(fake)
        results = extractor.extract("hello", "hi")
        assert results == []

    def test_handles_empty_array(self):
        """空数组应返回空列表。"""
        fake = FakeLLMClient(fixed_content="[]")
        extractor = UnifiedMemoryExtractor(fake)
        results = extractor.extract("chat about weather", "sunny")
        assert results == []
