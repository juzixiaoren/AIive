"""基于模型的记忆提取信号分类。"""

from __future__ import annotations

import json
import logging
from typing import Any, ClassVar, Literal

from json_repair import repair_json
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aiive.core.llm_client import LLMClient
from aiive.core.text_utils import strip_code_fence
from aiive.memory.extraction_policy import MemorySignalAction
from aiive.prompts import get_prompt_registry

logger = logging.getLogger(__name__)


# ============================================================================
# MemorySignalDecision — model-based extraction signal (not keywords)
# ============================================================================


class MemorySignalDecision(BaseModel):
    """Model-classified memory extraction signal.

    Produced by classify_memory_signal().
    No keyword matching, no length heuristics, no hardcoded marker lists.
    """
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)

    action: Literal["skip", "extract_async", "extract_sync"] = MemorySignalAction.EXTRACT_ASYNC.value
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    reason: str = ""


# ============================================================================
# ActionPlanner
# ============================================================================


class ActionPlanner:
    """Lightweight planner: intent extraction (for logging) + memory signal classifier.

    Does NOT perform tool dispatch.
    """

    def __init__(self, llm_client: LLMClient):
        self._llm: LLMClient = llm_client

    def classify_memory_signal(
        self,
        user_message: str,
        reply: str,
        trace_id: str | None = None,
    ) -> MemorySignalDecision:
        """Use cheap model call to classify memory extraction signal.

        Args:
            user_message: User's message.
            reply: Assistant's reply.
            trace_id: Trace ID.

        Returns:
            MemorySignalDecision with skip/extract_async/extract_sync.
        """
        prompt = get_prompt_registry().render(
            "memory.signal_classifier",
            user_message=user_message,
            reply=reply,
        ).content
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

        try:
            # Use a cheap model call with low temperature for classification
            from aiive.core.llm_client import LLMResponse
            for attempt in range(2):
                response: LLMResponse = self._llm.chat(
                    messages, trace_id=trace_id, temperature=0.0, json_mode=True,
                )
                if response.finish_reason == "length":
                    error_text = "JSON output was truncated"
                else:
                    try:
                        return self._parse_signal(response.content)
                    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as error:
                        error_text = self._validation_feedback(error)
                if attempt == 0:
                    messages = [
                        *messages,
                        {"role": "assistant", "content": response.content},
                        {
                            "role": "user",
                            "content": get_prompt_registry().render(
                                "memory.signal_retry",
                                error_text=error_text,
                            ).content,
                        },
                    ]
        except Exception:
            logger.warning("记忆信号分类失败，回退为 extract_async: trace_id=%s", trace_id, exc_info=True)
        return MemorySignalDecision(
            action=MemorySignalAction.EXTRACT_ASYNC.value,
            confidence=0.3,
            reason="Classifier output failed validation, defaulting to extract_async",
        )

    @staticmethod
    def _parse_signal(raw: str) -> MemorySignalDecision:
        """Parse model output into MemorySignalDecision."""
        text = strip_code_fence(raw)
        try:
            data: Any = json.loads(text)
        except json.JSONDecodeError:
            data = json.loads(repair_json(text))
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return MemorySignalDecision.model_validate(data)

    @staticmethod
    def _validation_feedback(error: Exception) -> str:
        if isinstance(error, ValidationError):
            parts = [
                f"{'.'.join(str(p) for p in item['loc'])}: {item['msg']}"
                for item in error.errors(include_url=False, include_input=False)[:6]
            ]
            return "; ".join(parts)
        return str(error)[:300]
