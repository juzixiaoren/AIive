"""记忆/知识库向量使用的 OpenAI-compatible Embeddings 客户端。"""
from __future__ import annotations

from typing import Any, Literal

import httpx

InputType = Literal["query", "document"]


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
        input_type: InputType = "document",
    ) -> list[float]:
        """为单段文本生成向量，并严格校验响应维度。"""
        if not text.strip():
            raise ValueError("Embedding 输入不能为空")
        embedding_text = self._prepare_text(text, input_type)
        payload = self._post([embedding_text])
        return self._parse(payload, expected=1)[0]

    def embed_batch(
        self,
        texts: list[str],
        *,
        input_type: InputType = "document",
        batch_size: int = 8,
    ) -> list[list[float]]:
        """按批生成向量；顺序与输入一致，逐条校验维度。

        TEI 的 `max_batch_requests` 受后端限制（cpu 后端通常 ≤4），批次过大
        只会被服务端拒绝或排队；调用方用 `batch_size` 控制并发与内存峰值。
        """
        if not texts:
            return []
        if batch_size <= 0:
            raise ValueError("Embedding batch_size 必须大于 0")
        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            window = texts[start : start + batch_size]
            for text in window:
                if not text.strip():
                    raise ValueError("Embedding 输入不能为空")
            prepared = [self._prepare_text(text, input_type) for text in window]
            payload = self._post(prepared)
            vectors.extend(self._parse(payload, expected=len(prepared)))
        return vectors

    # ── 内部实现 ────────────────────────────────────────────────

    def _prepare_text(self, text: str, input_type: InputType) -> str:
        """按输入类型拼接查询指令前缀（本地 BGE 的非对称检索要求）。"""
        if input_type == "query" and self._query_prefix:
            return f"{self._query_prefix}{text}"
        return text

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key.strip():
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _post(self, inputs: list[str]) -> Any:
        """发送一次请求：单条输入仍以字符串提交，保持既有契约。"""
        request_payload: dict[str, object] = {
            "model": self._model,
            "input": inputs[0] if len(inputs) == 1 else inputs,
            "encoding_format": "float",
        }
        # TEI 的维度由模型固定输出；远端 OpenAI-compatible API 则允许显式请求维度。
        if self._provider == "openai_compatible":
            request_payload["dimensions"] = self._dimensions
        try:
            response = httpx.post(
                self._url,
                headers=self._headers(),
                json=request_payload,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingProviderError(f"Embedding 服务调用失败: {exc}") from exc

    def _parse(self, payload: Any, *, expected: int) -> list[list[float]]:
        """校验响应条目数、维度，并按 index 还原输入顺序。"""
        try:
            rows = payload["data"]
        except (KeyError, TypeError) as exc:
            raise EmbeddingProviderError(f"Embedding 服务调用失败: {exc}") from exc
        if not isinstance(rows, list) or len(rows) != expected:
            actual = len(rows) if isinstance(rows, list) else 0
            raise EmbeddingProviderError(
                f"Embedding 响应条数不匹配: expected={expected}, actual={actual}"
            )
        ordered: list[list[float] | None] = [None] * expected
        for position, row in enumerate(rows):
            try:
                index = row.get("index", position)
                vector = row["embedding"]
            except (AttributeError, KeyError, TypeError) as exc:
                raise EmbeddingProviderError(f"Embedding 服务调用失败: {exc}") from exc
            if not isinstance(index, int) or not 0 <= index < expected:
                raise EmbeddingProviderError(f"Embedding 响应 index 非法: {index}")
            if not isinstance(vector, list) or len(vector) != self._dimensions:
                actual = len(vector) if isinstance(vector, list) else 0
                raise EmbeddingProviderError(
                    f"Embedding 维度不匹配: expected={self._dimensions}, actual={actual}"
                )
            ordered[index] = [float(value) for value in vector]
        if any(item is None for item in ordered):
            raise EmbeddingProviderError("Embedding 响应缺少条目")
        return [item for item in ordered if item is not None]
