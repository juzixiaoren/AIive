import json

from aiive.core.llm_client import FakeLLMClient
from aiive.memory.steward_signal_extractor import StewardSignalExtractor


class TestStewardSignalExtractor:
    def test_extract_routine_signal(self):
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
        extractor = StewardSignalExtractor(fake)
        results = extractor.extract("我每天早上7点去健身房", "好的")
        assert len(results) == 1
        assert results[0]["signal_type"] == "routine"
        assert results[0]["schedule_text"] == "daily at 7am"

    def test_extract_preference_signal(self):
        fake = FakeLLMClient(
            fixed_content=json.dumps([
                {
                    "signal_type": "preference",
                    "content": "User prefers working in dark mode",
                    "confidence": 0.8,
                }
            ]),
        )
        extractor = StewardSignalExtractor(fake)
        results = extractor.extract("我喜欢暗色模式", "了解了")
        assert len(results) == 1
        assert results[0]["signal_type"] == "preference"

    def test_extract_profile_signal(self):
        fake = FakeLLMClient(
            fixed_content=json.dumps([
                {
                    "signal_type": "user_profile",
                    "content": "User is a software engineer in Beijing",
                    "confidence": 0.95,
                }
            ]),
        )
        extractor = StewardSignalExtractor(fake)
        results = extractor.extract("我是北京的软件工程师", "收到")
        assert len(results) == 1
        assert results[0]["signal_type"] == "user_profile"

    def test_filters_invalid_signal_types(self):
        fake = FakeLLMClient(
            fixed_content=json.dumps([
                {"signal_type": "routine", "content": "valid", "confidence": 0.9},
                {"signal_type": "unknown", "content": "bad", "confidence": 0.1},
            ])
        )
        extractor = StewardSignalExtractor(fake)
        results = extractor.extract("test", "ok")
        assert len(results) == 1
        assert results[0]["signal_type"] == "routine"

    def test_handles_invalid_json(self):
        fake = FakeLLMClient(fixed_content="not json")
        extractor = StewardSignalExtractor(fake)
        results = extractor.extract("hello", "hi")
        assert results == []

    def test_handles_empty_array(self):
        fake = FakeLLMClient(fixed_content="[]")
        extractor = StewardSignalExtractor(fake)
        results = extractor.extract("chat about weather", "sunny")
        assert results == []
