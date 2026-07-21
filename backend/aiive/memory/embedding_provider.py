"""记忆向量使用的真实 OpenAI Embeddings 客户端。"""
from __future__ import annotations

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
    ) -> None:
        if not api_key.strip():
            raise ValueError("启用记忆向量能力时必须配置 AIIVE_EMBEDDING_API_KEY")
        if dimensions <= 0:
            raise ValueError("AIIVE_EMBEDDING_DIMENSIONS 必须大于 0")
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/embeddings"
        self._model = model
        self._dimensions = dimensions
        self._timeout_seconds = timeout_seconds

    @property
    def dimensions(self) -> int:
        """返回当前 provider 输出的固定向量维度。"""
        return self._dimensions

    def embed(self, text: str) -> list[float]:
        """为单段文本生成向量，并严格校验响应维度。"""
        if not text.strip():
            raise ValueError("Embedding 输入不能为空")
        try:
            response = httpx.post(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "input": text,
                    "dimensions": self._dimensions,
                    "encoding_format": "float",
                },
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            vector = payload["data"][0]["embedding"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise EmbeddingProviderError(f"OpenAI Embeddings 调用失败: {exc}") from exc
        if not isinstance(vector, list) or len(vector) != self._dimensions:
            actual = len(vector) if isinstance(vector, list) else 0
            raise EmbeddingProviderError(
                f"Embedding 维度不匹配: expected={self._dimensions}, actual={actual}"
            )
        return [float(value) for value in vector]
