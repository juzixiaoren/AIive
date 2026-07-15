"""Phase 1: TokenCounter — 基于 LiteLLM，带保守 fallback。

ContextAssembler 仅依赖 TokenCounter Protocol，永不直接依赖 LiteLLM。
"""
from __future__ import annotations

import json as _json
import logging
import math
from typing import Any, cast, Protocol, runtime_checkable

from aiive.runtime.token_models import (
    ModelProfile,
    TokenCount,
    TokenSafetyConfig,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class TokenCounter(Protocol):
    """抽象 token 计数接口。

    ContextAssembler 依赖此 Protocol，永不直接依赖 LiteLLM。
    """
    def count_messages(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> TokenCount: ...


# ============================================================================
# LiteLLMTokenCounter
# ============================================================================


class LiteLLMTokenCounter:
    """基于 LiteLLM token_counter 的默认 TokenCounter 实现。

    失败时回退到 ConservativeFallbackCounter。
    """

    def __init__(self, safety: TokenSafetyConfig | None = None, profile: ModelProfile | None = None):
        self._safety: TokenSafetyConfig = safety or TokenSafetyConfig()
        self._profile: ModelProfile = profile or ModelProfile.from_config("deepseek", "deepseek-chat")
        self._fallback: _ConservativeFallbackEstimator = _ConservativeFallbackEstimator(self._safety, self._profile)

    def count_messages(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> TokenCount:
        try:
            from litellm import token_counter as litellm_count
            estimated = litellm_count(model=model, messages=messages, tools=cast(Any, tools))
            if estimated <= 0:
                raise ValueError(f"LiteLLM returned {estimated}")
            margin = max(
                math.ceil(estimated * self._safety.litellm_margin_ratio),
                self._safety.litellm_min_margin_tokens,
            )
            return TokenCount(
                estimated_tokens=estimated,
                safety_margin_tokens=margin,
                source="litellm",
                confidence="high",
                model=model,
            )
        except Exception as e:
            logger.warning("LiteLLM token_counter 失败 (%s)，使用保守 fallback", e)
            return self._fallback.estimate(model, messages, tools, error=str(e))


# ============================================================================
# _ConservativeFallbackEstimator
# ============================================================================


class _ConservativeFallbackEstimator:
    """有界保守 fallback：对完整请求进行 canonical JSON 序列化后统一估算。

    不使用手工枚举 message.content/tool_calls。
    使用 math.ceil、保守倍率，永不返回 0。
    """

    def __init__(self, safety: TokenSafetyConfig, _profile: ModelProfile):
        self._safety: TokenSafetyConfig = safety

    def estimate(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        error: str = "",
    ) -> TokenCount:
        # 完整请求结构的 canonical JSON 序列化
        canonical = _json.dumps({
            "messages": messages,
            "tools": tools or [],
        }, ensure_ascii=False, sort_keys=True, default=str)

        # UTF-8 字节数 → token 估算（保守：约 1.6 字节/token）
        byte_count = len(canonical.encode("utf-8"))
        estimated = max(1, math.ceil(byte_count / 1.6))

        margin = max(
            math.ceil(estimated * self._safety.fallback_margin_ratio),
            self._safety.fallback_min_margin_tokens,
        )

        logger.info(
            "保守 fallback: estimated=%d margin=%d safe=%d error=%s model=%s",
            estimated, margin, estimated + margin, error, model,
        )

        return TokenCount(
            estimated_tokens=estimated,
            safety_margin_tokens=margin,
            source="conservative_fallback",
            confidence="low",
            model=model,
        )
