"""Global input budget. TokenCounter already includes the safety margin."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_warned_deprecated_override = False


@dataclass(frozen=True)
class ContextBudget:
    model_context_window: int
    reserved_output_tokens: int

    def __post_init__(self) -> None:
        if not 0 <= self.reserved_output_tokens < self.model_context_window:
            raise ValueError("reserved output must be smaller than the model context window")

    @property
    def hard_input_limit(self) -> int:
        return self.model_context_window - self.reserved_output_tokens

    @classmethod
    def default(cls) -> "ContextBudget":
        """唯一的权威预算入口：全局模型窗口 + 预留输出。"""
        from aiive.config import settings
        return cls(settings.aiive_llm_context_window, settings.aiive_llm_max_output_tokens)


def warn_deprecated_budget_override() -> None:
    """启动期一次性提示：局部预算覆盖已废弃且不再生效。"""
    global _warned_deprecated_override
    if _warned_deprecated_override or "AIIVE_CONTEXT_BUDGET_JSON" not in os.environ:
        return
    _warned_deprecated_override = True
    logger.warning("AIIVE_CONTEXT_BUDGET_JSON is deprecated and ignored; only the global model window applies")
