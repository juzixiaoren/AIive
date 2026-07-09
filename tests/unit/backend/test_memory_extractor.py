"""测试 MemoryExtractor 记忆提取器的解析和容错能力。"""
import json

from aiive.core.llm_client import FakeLLMClient
from aiive.memory.memory_extractor import MemoryExtractor


class TestMemoryExtractor:
    """测试从对话中提取记忆的各种场景。"""

    def test_extract_parses_valid_json(self):
        """验证能正确解析 LLM 返回的合法 JSON 格式记忆。"""
        fake = FakeLLMClient(
            fixed_content=json.dumps([
                {"content": "User loves pizza", "memory_type": "preference", "confidence": 0.9}
            ]),
        )
        extractor = MemoryExtractor(fake)
        results = extractor.extract("I love pizza", "Great!")
        assert len(results) == 1
        assert results[0]["content"] == "User loves pizza"
        assert results[0]["memory_type"] == "preference"
        assert results[0]["confidence"] == 0.9

    def test_extract_handles_empty_array(self):
        """验证空数组结果返回空列表。"""
        fake = FakeLLMClient(fixed_content="[]")
        extractor = MemoryExtractor(fake)
        results = extractor.extract("Hi", "Hello!")
        assert results == []

    def test_extract_handles_markdown_json_block(self):
        """验证能正确解析 Markdown 代码块包裹的 JSON。"""
        fake = FakeLLMClient(
            fixed_content="```json\n[{\"content\": \"User likes tea\", \"memory_type\": \"preference\", \"confidence\": 0.8}]\n```"
        )
        extractor = MemoryExtractor(fake)
        results = extractor.extract("I like tea", "Noted")
        assert len(results) == 1

    def test_extract_handles_invalid_json(self):
        """验证非法 JSON 时返回空列表而不抛异常。"""
        fake = FakeLLMClient(fixed_content="not json at all")
        extractor = MemoryExtractor(fake)
        results = extractor.extract("hello", "hi")
        assert results == []

    def test_extract_multiple_items(self):
        """验证能正确提取多条记忆。"""
        fake = FakeLLMClient(
            fixed_content=json.dumps([
                {"content": "User wakes at 7am", "memory_type": "schedule", "confidence": 0.95},
                {"content": "User prefers dark mode", "memory_type": "preference", "confidence": 0.7},
            ])
        )
        extractor = MemoryExtractor(fake)
        results = extractor.extract("I wake at 7 and prefer dark mode", "Ok")
        assert len(results) == 2
