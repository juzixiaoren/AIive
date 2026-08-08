"""记忆向量使用的 OpenAI-compatible Embeddings 客户端。"""
from __future__ import annotations

from typing import Literal

import httpx


class EmbeddingProviderError(RuntimeError):
    """Embedding 服务返回无效响应或调用失败。"""


class OpenAIEmbeddingProvider:
    """通过 OpenAI-compatible `/embeddings` 接口生成固定维度向量。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        dimensions: int,
        timeout_seconds: int,
        provider: Literal["local", "openai_compatible"] = "openai_compatible",
        query_prefix: str = "",
    ) -> None:
        if provider == "openai_compatible" and not api_key.strip():
            raise ValueError("启用记忆向量能力时必须配置 AIIVE_EMBEDDING_API_KEY")
        if dimensions <= 0:
            raise ValueError("AIIVE_EMBEDDING_DIMENSIONS 必须大于 0")
        self._api_key: str = api_key
        self._url: str = f"{base_url.rstrip('/')}/embeddings"
        self._model: str = model
        self._dimensions: int = dimensions
        self._timeout_seconds: int = timeout_seconds
        self._provider: Literal["local", "openai_compatible"] = provider
        self._query_prefix: str = query_prefix

    @property
    def dimensions(self) -> int:
        """返回当前 provider 输出的固定向量维度。"""
        return self._dimensions

    def embed(
        self,
        text: str,
        *,
        input_type: Literal["query", "document"] = "document",
    ) -> list[float]:
        """为单段文本生成向量，并严格校验响应维度。"""
        if not text.strip():
            raise ValueError("Embedding 输入不能为空")
        embedding_text = text
        if input_type == "query" and self._query_prefix:
            embedding_text = f"{self._query_prefix}{text}"
        headers = {"Content-Type": "application/json"}
        if self._api_key.strip():
            headers["Authorization"] = f"Bearer {self._api_key}"
        request_payload: dict[str, object] = {
            "model": self._model,
            "input": embedding_text,
            "encoding_format": "float",
        }
        # TEI 的维度由模型固定输出；远端 OpenAI-compatible API 则允许显式请求维度。
        if self._provider == "openai_compatible":
            request_payload["dimensions"] = self._dimensions
        try:
            response = httpx.post(
                self._url,
                headers=headers,
                json=request_payload,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            vector = payload["data"][0]["embedding"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise EmbeddingProviderError(f"Embedding 服务调用失败: {exc}") from exc
        if not isinstance(vector, list) or len(vector) != self._dimensions:
            actual = len(vector) if isinstance(vector, list) else 0
            raise EmbeddingProviderError(
                f"Embedding 维度不匹配: expected={self._dimensions}, actual={actual}"
            )
        return [float(value) for value in vector]
