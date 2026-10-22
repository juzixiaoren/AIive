"""LLM 客户端契约测试（完全离线：httpx.post 由可控响应替换）。

覆盖两层：
- FakeLLMClient 的固定响应与调用记录；
- 生产 LLMClient 的真实请求构造、响应解析与错误归一化（不访问外网、
  不需要 API Key、不消耗第三方额度）。
"""
from unittest.mock import MagicMock, patch

import httpx
import pytest

from aiive.core.llm_client import (
    FakeLLMClient,
    LLMClient,
    LLMClientError,
    LLMResponse,
    normalize_llm_error,
)
from aiive.memory.embedding_provider import EmbeddingProviderError, OpenAIEmbeddingProvider


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

    def test_normalizes_openai_and_deepseek_prompt_cache_usage(self):
        openai_usage = LLMClient._normalize_usage({  # pyright: ignore[reportPrivateUsage]
            "prompt_tokens": 120,
            "completion_tokens": 8,
            "total_tokens": 128,
            "prompt_tokens_details": {"cached_tokens": 96},
        })
        assert openai_usage["cache_read_tokens"] == 96

        deepseek_usage = LLMClient._normalize_usage({  # pyright: ignore[reportPrivateUsage]
            "prompt_tokens": 120,
            "completion_tokens": 8,
            "total_tokens": 128,
            "prompt_cache_hit_tokens": 80,
            "prompt_cache_miss_tokens": 40,
        })
        assert deepseek_usage == {
            "prompt_tokens": 120,
            "completion_tokens": 8,
            "total_tokens": 128,
            "cache_read_tokens": 80,
            "cache_write_tokens": 0,
        }

    def test_usage_without_cache_fields_keeps_three_field_contract(self):
        usage = LLMClient._normalize_usage({  # pyright: ignore[reportPrivateUsage]
            "prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7,
        })
        assert usage == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}

    def test_usage_tolerates_missing_and_malformed_values(self):
        usage = LLMClient._normalize_usage({  # pyright: ignore[reportPrivateUsage]
            "prompt_tokens": "12", "completion_tokens": None, "total_tokens": "bad",
        })
        assert usage == {"prompt_tokens": 12, "completion_tokens": 0, "total_tokens": 0}


class TestLLMClientRequestConstruction:
    """请求构造：URL / 请求头 / payload 均使用可控响应验证，不发起真实请求。"""

    @staticmethod
    def _response(status: int, payload: dict | None = None, text: str = "") -> MagicMock:
        response = MagicMock()
        response.status_code = status
        response.text = text
        response.json.return_value = payload or {}
        return response

    @patch("aiive.core.llm_client.httpx.post")
    def test_posts_to_chat_completions_with_auth_and_model(self, post: MagicMock):
        post.return_value = self._response(200, {
            "model": "m1",
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }, text='{"choices":[]}')
        client = LLMClient("https://provider.example/v1/", "secret-key", "default-model")

        response = client.chat([{"role": "user", "content": "你好"}], trace_id="trace-1")

        assert response.content == "hi"
        assert response.trace_id == "trace-1"
        assert response.finish_reason == "stop"
        assert response.usage == {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        kwargs = post.call_args.kwargs
        assert post.call_args.args[0] == "https://provider.example/v1/chat/completions"
        assert kwargs["headers"]["Authorization"] == "Bearer secret-key"
        assert kwargs["headers"]["Content-Type"] == "application/json"
        assert kwargs["json"]["model"] == "default-model"
        assert kwargs["json"]["messages"] == [{"role": "user", "content": "你好"}]
        # 未传 temperature 时不得注入该字段
        assert "temperature" not in kwargs["json"]
        assert "response_format" not in kwargs["json"]

    @patch("aiive.core.llm_client.httpx.post")
    def test_temperature_and_model_override_are_forwarded(self, post: MagicMock):
        post.return_value = self._response(200, {
            "model": "override-model",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "length"}],
        }, text="{}")
        client = LLMClient("https://provider.example/v1", "k", "default-model")

        response = client.chat(
            [{"role": "user", "content": "hi"}], model="override-model", temperature=0.25,
        )

        payload = post.call_args.kwargs["json"]
        assert payload["model"] == "override-model"
        assert payload["temperature"] == 0.25
        # 响应缺失 usage 时回退三字段零值契约
        assert response.usage == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    @patch("aiive.core.llm_client.httpx.post")
    def test_json_mode_off_policy_never_sends_response_format(self, post: MagicMock):
        post.return_value = self._response(200, {
            "model": "m", "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        }, text="{}")
        client = LLMClient(
            "https://provider.example/v1", "k", "m", json_mode_policy="off",
        )

        client.chat([{"role": "user", "content": "JSON"}], json_mode=True)

        assert "response_format" not in post.call_args.kwargs["json"]

    @patch("aiive.core.llm_client.httpx.post")
    def test_auto_policy_retries_without_downgrade_on_generic_400(self, post: MagicMock):
        """400 但未提及 response_format 不支持时不得降级，直接按错误处理。"""
        post.return_value = self._response(400, text="invalid messages format")
        client = LLMClient("https://provider.example/v1", "k", "m")

        with pytest.raises(LLMClientError) as excinfo:
            client.chat([{"role": "user", "content": "hi"}], json_mode=True)

        assert excinfo.value.status_code == 400
        assert post.call_count == 1
        assert post.call_args.kwargs["json"]["response_format"] == {"type": "json_object"}


class TestLLMClientErrorHandling:
    """错误处理：HTTP 状态码、超时与网络异常的归一化契约（离线）。"""

    @staticmethod
    def _response(status: int, text: str = "") -> MagicMock:
        response = MagicMock()
        response.status_code = status
        response.text = text
        response.json.return_value = {}
        return response

    @patch("aiive.core.llm_client.httpx.post")
    def test_non_200_raises_with_status_and_body_preview(self, post: MagicMock):
        post.return_value = self._response(429, text="rate limited")
        client = LLMClient("https://provider.example/v1", "k", "m")

        with pytest.raises(LLMClientError) as excinfo:
            client.chat([{"role": "user", "content": "hi"}], trace_id="t-429")

        assert excinfo.value.status_code == 429
        assert excinfo.value.trace_id == "t-429"
        assert "429" in str(excinfo.value)
        assert "rate limited" in str(excinfo.value)

    @patch("aiive.core.llm_client.httpx.post")
    def test_timeout_raises_llmclienterror_without_status(self, post: MagicMock):
        post.side_effect = httpx.TimeoutException("read timeout")
        client = LLMClient("https://provider.example/v1", "k", "m", timeout_seconds=7)

        with pytest.raises(LLMClientError) as excinfo:
            client.chat([{"role": "user", "content": "hi"}], trace_id="t-timeout")

        assert excinfo.value.status_code is None
        assert "timed out after 7s" in str(excinfo.value)

    @patch("aiive.core.llm_client.httpx.post")
    def test_network_error_is_wrapped_not_leaked(self, post: MagicMock):
        post.side_effect = httpx.ConnectError("connection refused")
        client = LLMClient("https://provider.example/v1", "k", "m")

        with pytest.raises(LLMClientError) as excinfo:
            client.chat([{"role": "user", "content": "hi"}])

        assert "LLM request failed" in str(excinfo.value)

    @patch("aiive.core.llm_client.httpx.post")
    def test_timeout_parameter_overrides_default(self, post: MagicMock):
        post.return_value = self._response(200)
        post.return_value.json.return_value = {
            "model": "m", "choices": [{"message": {"content": "x"}}],
        }
        client = LLMClient("https://provider.example/v1", "k", "m", timeout_seconds=30)

        client.chat([{"role": "user", "content": "hi"}], timeout=3)

        assert post.call_args.kwargs["timeout"].read == 3


class TestNormalizeLLMError:
    """错误分类契约：调用方依赖 code / retryable 决定重试与降级。"""

    def test_rate_limit_is_retryable(self):
        # 契约：限流仅按 status_code==429 或异常类名含 "ratelimit" 识别，
        # 不解析异常消息文本（消息文案不稳定，避免误判）。
        class RateLimitError(Exception):
            pass

        error = normalize_llm_error(RateLimitError("slow down"))
        assert error.code == "llm_rate_limited"
        assert error.retryable is True
        assert error.status_code == 429

    def test_rate_limit_detected_by_status_code(self):
        class _WithStatus(Exception):
            status_code = 429

        error = normalize_llm_error(_WithStatus())
        assert error.code == "llm_rate_limited"
        assert error.retryable is True

    def test_timeout_name_maps_to_llm_timeout(self):
        error = normalize_llm_error(TimeoutError())
        assert error.code == "llm_timeout"
        assert error.retryable is True

    def test_auth_failure_is_not_retryable(self):
        error = normalize_llm_error(RuntimeError("boom"), trace_id="t")
        assert error.code == "llm_request_failed"
        assert error.retryable is True

        class _WithStatus(Exception):
            status_code = 401

        auth_error = normalize_llm_error(_WithStatus())
        assert auth_error.code == "llm_configuration_error"
        assert auth_error.retryable is False
        assert auth_error.status_code == 503

    def test_server_error_is_retryable(self):
        class _WithResponse(Exception):
            response = MagicMock(status_code=503)

        error = normalize_llm_error(_WithResponse())
        assert error.code == "llm_unavailable"
        assert error.retryable is True

    def test_existing_error_gets_trace_id_backfilled(self):
        original = LLMClientError("already normalized", code="custom", retryable=False)
        error = normalize_llm_error(original, trace_id="t-9")
        assert error is original
        assert error.trace_id == "t-9"
        assert error.code == "custom"
        assert error.retryable is False


class TestEmbeddingProviderOfflineErrors:
    """Embedding 客户端离线错误路径（原真实 API 测试的价值改造）。"""

    def _provider(self, **kwargs) -> OpenAIEmbeddingProvider:
        params = {
            "api_key": "k", "base_url": "https://emb.example/v1",
            "model": "m", "dimensions": 4, "timeout_seconds": 5,
        }
        params.update(kwargs)
        return OpenAIEmbeddingProvider(**params)

    def test_empty_input_rejected_before_request(self, monkeypatch):
        called = False

        def fail_post(*_a, **_k):
            nonlocal called
            called = True
            raise AssertionError("空输入必须在发起请求前被拒绝")

        monkeypatch.setattr(httpx, "post", fail_post)
        with pytest.raises(ValueError, match="不能为空"):
            self._provider().embed("   ")
        assert called is False

    def test_http_error_becomes_provider_error(self, monkeypatch):
        def raise_http_error(*_a, **_k):
            raise httpx.HTTPError("502 bad gateway")

        monkeypatch.setattr(httpx, "post", raise_http_error)
        with pytest.raises(EmbeddingProviderError, match="Embedding 服务调用失败"):
            self._provider().embed("text")

    def test_dimension_mismatch_becomes_provider_error(self, monkeypatch):
        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"data": [{"embedding": [0.1, 0.2]}]}

        monkeypatch.setattr(httpx, "post", lambda *_a, **_k: _Response())
        with pytest.raises(EmbeddingProviderError, match="维度不匹配"):
            self._provider().embed("text")

    def test_malformed_payload_becomes_provider_error(self, monkeypatch):
        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"unexpected": True}

        monkeypatch.setattr(httpx, "post", lambda *_a, **_k: _Response())
        with pytest.raises(EmbeddingProviderError):
            self._provider().embed("text")
