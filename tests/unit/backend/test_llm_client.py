import pytest

from aiive.core.llm_client import FakeLLMClient, LLMClientError, LLMResponse


class TestFakeLLMClient:
    def test_returns_fixed_content(self):
        client = FakeLLMClient(fixed_content="Bonjour")
        response = client.chat([{"role": "user", "content": "Hello"}])
        assert response.content == "Bonjour"

    def test_returns_default_content(self):
        client = FakeLLMClient()
        response = client.chat([{"role": "user", "content": "Hi"}])
        assert response.content == "Hello from FakeLLM"

    def test_response_includes_model(self):
        client = FakeLLMClient(fixed_model="gpt-test")
        response = client.chat([{"role": "user", "content": "Hi"}])
        assert response.model == "gpt-test"

    def test_response_includes_usage(self):
        client = FakeLLMClient(
            fixed_usage={"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10}
        )
        response = client.chat([{"role": "user", "content": "Hi"}])
        assert response.usage == {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10}

    def test_response_includes_latency(self):
        client = FakeLLMClient(latency_ms=42.0)
        response = client.chat([{"role": "user", "content": "Hi"}])
        assert response.latency_ms == 42.0

    def test_response_includes_trace_id(self):
        client = FakeLLMClient()
        response = client.chat([{"role": "user", "content": "Hi"}])
        assert response.trace_id
        assert len(response.trace_id) > 0

    def test_accepts_custom_trace_id(self):
        client = FakeLLMClient()
        response = client.chat(
            [{"role": "user", "content": "Hi"}],
            trace_id="my-trace-123",
        )
        assert response.trace_id == "my-trace-123"

    def test_trace_id_is_unique_per_call(self):
        client = FakeLLMClient()
        r1 = client.chat([{"role": "user", "content": "A"}])
        r2 = client.chat([{"role": "user", "content": "B"}])
        assert r1.trace_id != r2.trace_id

    def test_records_call_history(self):
        client = FakeLLMClient()
        client.chat([{"role": "user", "content": "Q1"}])
        client.chat([{"role": "user", "content": "Q2"}], temperature=0.5)
        assert len(client._call_history) == 2
        assert client._call_history[0]["messages"][0]["content"] == "Q1"
        assert client._call_history[1]["temperature"] == 0.5

    def test_accepts_model_override(self):
        client = FakeLLMClient(fixed_model="base-model")
        response = client.chat([{"role": "user", "content": "Hi"}], model="override-model")
        assert response.model == "override-model"

    def test_response_is_llmresponse_instance(self):
        client = FakeLLMClient()
        response = client.chat([{"role": "user", "content": "Hi"}])
        assert isinstance(response, LLMResponse)

    def test_raw_preview_is_populated(self):
        client = FakeLLMClient()
        response = client.chat([{"role": "user", "content": "Hi"}])
        assert len(response.raw_preview) > 0
