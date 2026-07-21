"""
ActionPlanner: lightweight structured decision extraction.

- AgentDecision: the main agent's structured output (logging + memory signal)
- classify_memory_signal(): cheap model call for memory extraction signal
  (SKIP / EXTRACT_ASYNC / EXTRACT_SYNC). Replaces keyword-based heuristics.

MemorySignalDecision is independent of intent_type and execution_mode.
Produced by a cheap model call after the main LLM reply.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from json_repair import repair_json
from pydantic import BaseModel, Field

from aiive.core.llm_client import LLMClient
from aiive.core.text_utils import strip_code_fence
from aiive.memory.extraction_policy import MemorySignalAction

logger = logging.getLogger(__name__)


# ============================================================================
# MemorySignalDecision — model-based extraction signal (not keywords)
# ============================================================================


class MemorySignalDecision(BaseModel):
    """Model-classified memory extraction signal.

    Produced by classify_memory_signal().
    No keyword matching, no length heuristics, no hardcoded marker lists.
    """
    action: str = Field(default=MemorySignalAction.EXTRACT_ASYNC.value)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    reason: str = ""


_MEMORY_SIGNAL_PROMPT: str = """Classify whether the following conversation turn contains information worth remembering long-term.

Output ONLY a JSON object:
{
  "action": "skip" | "extract_async" | "extract_sync",
  "confidence": 0.0-1.0,
  "reason": "brief explanation"
}

Rules:
- skip: pure greeting, simple acknowledgement, transient problem report ("my code errored"), small talk, one-off factual question. Do NOT extract.
- extract_async: contains preferences, facts, habits, project details, or general information that may be useful later. Enqueue for background processing.
- extract_sync: contains explicit identity changes, policy rules, or critical corrections that must be remembered immediately. Use sparingly.

Conversation:
User: {user_message}
Assistant: {reply}

Output ONLY valid JSON, no markdown:"""


# ============================================================================
# AgentDecision — lightweight structured output (logging only)
# ============================================================================


class AgentDecision(BaseModel):
    """Structured decision output — used for logging, NOT for tool dispatch.

    Tool dispatch is handled by LangGraph native tool_calls + policy_check.
    memory_signal is produced by classify_memory_signal() after main LLM reply.
    """
    decision_type: str = "final_response"
    execution_mode: str = "explain_only"
    intent_type: str = "normal_chat"
    should_execute: bool = False
    tool_name: str | None = None
    tool_params: dict[str, Any] = Field(default_factory=dict)
    args: dict[str, Any] = Field(default_factory=dict)
    requires_confirmation: bool = False
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    parse_failed: bool = False
    reason: str = ""
    # Memory extraction signal (model-classified, NOT keyword-based)
    memory_signal: MemorySignalDecision | None = None



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
        # 模板中含字面量 JSON 示例（裸花括号），不能用 str.format，
        # 否则会把 JSON 里的 {...} 当成格式字段而抛 KeyError。
        prompt = (
            _MEMORY_SIGNAL_PROMPT
            .replace("{user_message}", user_message)
            .replace("{reply}", reply)
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

        try:
            # Use a cheap model call with low temperature for classification
            from aiive.core.llm_client import LLMResponse
            response: LLMResponse = self._llm.chat(
                messages, trace_id=trace_id, temperature=0.0,
            )
            return self._parse_signal(response.content)
        except Exception:
            logger.warning("记忆信号分类失败，回退为 extract_async: trace_id=%s", trace_id, exc_info=True)
            # On failure: default to EXTRACT_ASYNC (conservative)
            return MemorySignalDecision(
                action=MemorySignalAction.EXTRACT_ASYNC.value,
                confidence=0.3,
                reason="Classifier failed, defaulting to extract_async",
            )

    @staticmethod
    def _parse_signal(raw: str) -> MemorySignalDecision:
        """Parse model output into MemorySignalDecision."""
        try:
            text = strip_code_fence(raw)
            try:
                data: dict[str, Any] = json.loads(text)
            except json.JSONDecodeError:
                data = json.loads(repair_json(text))
            action_raw = str(data.get("action", "extract_async")).lower()
            # Validate action
            valid_actions = {a.value for a in MemorySignalAction}
            if action_raw not in valid_actions:
                action_raw = MemorySignalAction.EXTRACT_ASYNC.value
            return MemorySignalDecision(
                action=action_raw,
                confidence=float(data.get("confidence", 0.5)),
                reason=str(data.get("reason", ""))[:200],
            )
        except (json.JSONDecodeError, ValueError, TypeError):
            return MemorySignalDecision(
                action=MemorySignalAction.EXTRACT_ASYNC.value,
                confidence=0.3,
                reason="Parse failed, defaulting to extract_async",
            )
