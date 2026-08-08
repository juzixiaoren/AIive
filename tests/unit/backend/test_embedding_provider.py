"""OpenAI-compatible / 本地 TEI embedding provider 回归测试。"""
from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from aiive.config import Settings
from aiive.memory.embedding_provider import OpenAIEmbeddingProvider


class _Response:
    def __init__(self, dimensions: int) -> None:
        self._dimensions = dimensions

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"data": [{"embedding": [0.01] * self._dimensions}]}


def test_local_tei_omits_auth_and_dimensions(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return _Response(512)

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = OpenAIEmbeddingProvider(
        api_key="",
        base_url="http://embedding:80/v1",
        model="BAAI/bge-small-zh-v1.5",
        dimensions=512,
        timeout_seconds=5,
        provider="local",
        query_prefix="查询：",
    )

    vector = provider.embed("用户偏好", input_type="query")

    assert len(vector) == 512
    assert captured["url"] == "http://embedding:80/v1/embeddings"
    assert "Authorization" not in captured["headers"]
    assert "dimensions" not in captured["json"]
    assert captured["json"]["input"] == "查询：用户偏好"


def test_remote_provider_sends_auth_and_dimensions(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return _Response(512)

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = OpenAIEmbeddingProvider(
        api_key="secret",
        base_url="https://example.test/v1",
        model="text-embedding-3-small",
        dimensions=512,
        timeout_seconds=5,
    )

    provider.embed("document")

    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["json"]["dimensions"] == 512
    assert captured["json"]["input"] == "document"


def test_local_vector_settings_do_not_require_api_key():
    configured = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://example",
        aiive_memory_vector_enabled=True,
        aiive_embedding_provider="local",
        aiive_embedding_api_key="",
        aiive_embedding_dimensions=512,
    )

    assert configured.aiive_embedding_provider == "local"


def test_remote_vector_settings_still_require_api_key():
    with pytest.raises(ValidationError, match="AIIVE_EMBEDDING_API_KEY"):
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://example",
            aiive_memory_vector_enabled=True,
            aiive_embedding_provider="openai_compatible",
            aiive_embedding_api_key="",
            aiive_embedding_dimensions=512,
        )
