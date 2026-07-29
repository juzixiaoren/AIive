"""测试 FakeLLMClient 和 LLMResponse 的各项功能。"""
from unittest.mock import MagicMock, patch

import pytest

from aiive.core.llm_client import FakeLLMClient, LLMClient, LLMClientError, LLMResponse


class TestFakeLLMClient:
    """测试 FakeLLMClient 模拟 LLM 客户端的各项行为。"""

    def test_returns_fixed_content(self):
        """验证返回预设的固定内容。"""
        client = FakeLLMClient(fixed_content="Bonjour")
        response = client.chat([{"role": "user", "content": "Hello"}])
        assert response.content == "Bonjour"

    def test_returns_default_content(self):
        """验证未指定内容时返回默认值。"""
        client = FakeLLMClient()
        response = client.chat([{"role": "user", "content": "Hi"}])
        assert response.content == "Hello from FakeLLM"

    def test_response_includes_model(self):
        """验证响应中包含指定的模型名称。"""
        client = FakeLLMClient(fixed_model="gpt-test")
        response = client.chat([{"role": "user", "content": "Hi"}])
        assert response.model == "gpt-test"

    def test_response_includes_usage(self):
        """验证响应中包含 token 用量统计。"""
        client = FakeLLMClient(
            fixed_usage={"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10}
        )
        response = client.chat([{"role": "user", "content": "Hi"}])
        assert response.usage == {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10}

    def test_response_includes_latency(self):
        """验证响应中包含延迟时间。"""
        client = FakeLLMClient(latency_ms=42.0)
        response = client.chat([{"role": "user", "content": "Hi"}])
        assert response.latency_ms == 42.0

    def test_response_includes_trace_id(self):
        """验证每次调用自动生成唯一的 trace_id。"""
        client = FakeLLMClient()
        response = client.chat([{"role": "user", "content": "Hi"}])
        assert response.trace_id
        assert len(response.trace_id) > 0

    def test_accepts_custom_trace_id(self):
        """验证支持传入自定义 trace_id。"""
        client = FakeLLMClient()
        response = client.chat(
            [{"role": "user", "content": "Hi"}],
            trace_id="my-trace-123",
        )
        assert response.trace_id == "my-trace-123"

    def test_trace_id_is_unique_per_call(self):
        """验证不同调用生成不同的 trace_id。"""
        client = FakeLLMClient()
        r1 = client.chat([{"role": "user", "content": "A"}])
        r2 = client.chat([{"role": "user", "content": "B"}])
        assert r1.trace_id != r2.trace_id

    def test_records_call_history(self):
        """验证正确记录调用历史。"""
        client = FakeLLMClient()
        client.chat([{"role": "user", "content": "Q1"}])
        client.chat([{"role": "user", "content": "Q2"}], temperature=0.5)
        assert len(client._call_history) == 2
        assert client._call_history[0]["messages"][0]["content"] == "Q1"
        assert client._call_history[1]["temperature"] == 0.5

    def test_accepts_model_override(self):
        """验证支持运行时覆盖模型名称。"""
        client = FakeLLMClient(fixed_model="base-model")
        response = client.chat([{"role": "user", "content": "Hi"}], model="override-model")
        assert response.model == "override-model"

    def test_response_is_llmresponse_instance(self):
        """验证返回值为 LLMResponse 实例。"""
        client = FakeLLMClient()
        response = client.chat([{"role": "user", "content": "Hi"}])
        assert isinstance(response, LLMResponse)

    def test_raw_preview_is_populated(self):
        """验证 raw_preview 字段已填充。"""
        client = FakeLLMClient()
        response = client.chat([{"role": "user", "content": "Hi"}])
        assert len(response.raw_preview) > 0

    def test_records_json_mode_request(self):
        client = FakeLLMClient()
        client.chat([{"role": "user", "content": "Return JSON"}], json_mode=True)
        assert client._call_history[0]["json_mode"] is True


class TestRealLLMClientJsonMode:
    @staticmethod
    def _response(status: int, payload: dict | None = None, text: str = "") -> MagicMock:
        response = MagicMock()
        response.status_code = status
        response.text = text
        response.json.return_value = payload or {}
        return response

    @patch("aiive.core.llm_client.httpx.post")
    def test_auto_falls_back_only_for_explicit_unsupported_error(self, post: MagicMock):
        post.side_effect = [
            self._response(400, text="response_format json_object is unsupported"),
            self._response(200, {
                "model": "portable-model",
                "choices": [{"message": {"content": "{\"ok\":true}"}, "finish_reason": "stop"}],
                "usage": {},
            }, text='{"choices":[]}'),
        ]
        client = LLMClient("https://provider.example/v1", "key", "portable-model")
        response = client.chat(
            [{"role": "user", "content": "Return JSON"}],
            json_mode=True,
        )
        assert response.content == '{"ok":true}'
        assert post.call_count == 2
        assert post.call_args_list[0].kwargs["json"]["response_format"] == {"type": "json_object"}
        assert "response_format" not in post.call_args_list[1].kwargs["json"]
