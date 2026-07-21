"""Phase 1: TokenCounter — 基于 LiteLLM，带保守 fallback。

ContextAssembler 仅依赖 TokenCounter Protocol，永不直接依赖 LiteLLM。
"""
from __future__ import annotations

import hashlib
import json as _json
import logging
import math
import threading
from collections import OrderedDict
from functools import lru_cache
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

    # 计数缓存上限（条目数）。token 计数对相同内容是纯函数，同一请求内的多次
    #   诊断报告编码、以及跨请求几乎不变的工具 schema / 稳定契约都能命中，
    #   避免对全量上下文反复做 tiktoken 编码（单请求 CPU 开销大头）。
    _CACHE_MAXSIZE: int = 512

    def __init__(self, safety: TokenSafetyConfig | None = None, profile: ModelProfile | None = None):
        self._safety: TokenSafetyConfig = safety or TokenSafetyConfig()
        self._profile: ModelProfile = profile or ModelProfile.from_config("deepseek", "deepseek-chat")
        self._fallback: _ConservativeFallbackEstimator = _ConservativeFallbackEstimator(self._safety, self._profile)
        # 内容 hash → TokenCount 的有界缓存。counter 通常以单例跨请求共享，
        # 故缓存需线程安全（并发 turn 可能同时调用）。
        self._cache: "OrderedDict[str, TokenCount]" = OrderedDict()
        self._cache_lock: threading.Lock = threading.Lock()

    @staticmethod
    def _cache_key(model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> str:
        canonical = _json.dumps(
            {"model": model, "messages": messages, "tools": tools or []},
            ensure_ascii=False, sort_keys=True, default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def count_messages(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> TokenCount:
        key = self._cache_key(model, messages, tools)
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached

        result = self._count_messages_uncached(model, messages, tools)

        with self._cache_lock:
            self._cache[key] = result
            self._cache.move_to_end(key)
            while len(self._cache) > self._CACHE_MAXSIZE:
                self._cache.popitem(last=False)
        return result

    def _count_messages_uncached(
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

    # ── 单段文本 token 计数（Phase 5 统一检索 token budget 打包）──

    @staticmethod
    def count_text(text: str, model: str = "deepseek/deepseek-chat") -> int:
        """对单段文本做真实 token 计数（返回 safe_tokens）。

        内部走 LiteLLM 真实计数 + fallback 保守估算；
        不使用 len//4 等启发式。

        复用进程级共享 counter（避免每次调用重建对象），并借助其内部缓存，
        使检索打包时对每个候选的重复计数可跨调用命中。
        """
        if not text:
            return 0
        tc = _shared_text_counter(model)
        result = tc.count_messages(
            model=model,
            messages=[{"role": "system", "content": text}],
        )
        return result.safe_tokens


# count_text 的进程级共享 counter（按 model 缓存）。避免检索热路径上
#   每个候选都新建 LiteLLMTokenCounter，并使其内部计数缓存跨调用生效。
@lru_cache(maxsize=8)
def _shared_text_counter(model: str) -> "LiteLLMTokenCounter":
    return LiteLLMTokenCounter(profile=ModelProfile.from_config("deepseek", model))


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
