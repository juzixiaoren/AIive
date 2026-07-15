"""Phase 1: Token 计数数据模型。

TokenCount、TokenSafetyConfig、ModelProfile、TokenEstimationRecord。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class TokenCount:
    """结构化 token 计数结果。

    safe_tokens = estimated_tokens + safety_margin_tokens
    source: "litellm" | "conservative_fallback"
    confidence: "high" | "low"
    """
    estimated_tokens: int
    safety_margin_tokens: int
    safe_tokens: int = 0
    source: str = "litellm"
    confidence: str = "high"
    model: str = ""

    def __post_init__(self) -> None:
        if self.safe_tokens == 0:
            object.__setattr__(self, "safe_tokens", self.estimated_tokens + self.safety_margin_tokens)


@dataclass(frozen=True)
class TokenSafetyConfig:
    """token 估算的安全余量配置。"""
    litellm_margin_ratio: float = 0.10
    litellm_min_margin_tokens: int = 500
    fallback_margin_ratio: float = 0.30
    fallback_min_margin_tokens: int = 1000


@dataclass(frozen=True)
class ModelProfile:
    """按 provider 感知的模型配置。

    full_name 使用 LiteLLM 格式："deepseek/deepseek-chat"、"openai/gpt-4o"。
    """
    provider: str
    model_id: str
    full_name: str
    context_window: int = 128000
    max_output_tokens: int = 4096
    min_output_tokens: int = 256

    @classmethod
    def from_config(cls, provider: str, model_id: str) -> "ModelProfile":
        full = f"{provider}/{model_id}"
        return cls(
            provider=provider,
            model_id=model_id,
            full_name=full,
        )


@dataclass
class TokenEstimationRecord:
    """调用后 token 估算对比记录（仅用于可观测性）。"""
    model: str
    estimated_tokens: int = 0
    safe_tokens: int = 0
    actual_prompt_tokens: int | None = None
    actual_completion_tokens: int | None = None
    source: str = ""
    confidence: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
