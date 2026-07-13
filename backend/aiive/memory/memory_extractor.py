"""Unified MemoryExtractor: single LLM call for all memory types.

Replaces MemoryExtractor (general) + StewardSignalExtractor (personal signals).
Outputs a list of normalized MemoryProposal via ProposalNormalizer.

Steward enrichment (routine, preference, habit detection) is integrated
into the single extraction prompt, not a second LLM call.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from json_repair import repair_json
from pydantic import BaseModel, Field

from aiive.core.llm_client import LLMClient, LLMResponse
from aiive.memory.memory_types import MEMORY_KEY_GUIDE, MemoryProposal, TrustLevel
from aiive.memory.proposal_normalizer import ProposalNormalizer, NormalizationResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ExtractedMemory (internal intermediate model)
# ---------------------------------------------------------------------------

class ExtractedMemory(BaseModel):
    """Single extracted memory item from LLM output."""
    content: str
    memory_type: str = ""
    memory_key: str = ""
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    source_span: str = ""
    durable: bool = True
    importance: float = Field(ge=0.0, le=1.0, default=0.5)
    signal_type: str = ""  # routine / preference / habit / schedule (steward enrichment)


# ---------------------------------------------------------------------------
# Unified extraction prompt (covers both MemoryExtractor + StewardSignalExtractor)
# ---------------------------------------------------------------------------

UNIFIED_EXTRACT_PROMPT = """从以下对话中提取持久性信息。输出 JSON 数组，每个对象包含：

- content: 事实/偏好/习惯/日程的简洁表述
- memory_type: 以下 canonical 类型之一：
    user_profile（身份信息、偏好、习惯、日程、规律）
    agent_self（Agent 的名称、人格、关系风格）
    project（项目决策、技术栈、架构选择）
    policy（规则、约束、禁止事项）
    procedural（工作流、执行方法、经验教训）
    episodic（值得记住的一次性事件）
    knowledge（通用事实和知识）
    environment（环境配置信息）
- memory_key: 去重用稳定键，规范见下方
- confidence: 0.0-1.0（对持久性的确信度）
- importance: 0.0-1.0（重要性）
- source_span: 用户消息中包含该事实的原文片段
- signal_type: 可选的管家信号标记（routine/habit/schedule/preference），无则留空

记忆键规范：
""" + MEMORY_KEY_GUIDE + """

关键规则：
- 只提取用户明确陈述的信息，不推断或猜测
- 身份键的 content 必须是纯值，不含前缀
- "我的代码报错了""今天好累""帮我看看"等暂时性情况不提取
- 用户明确要求"记住X""以后叫我X"的，confidence 设为 0.95+
- 用户陈述的日常规律（每天/每周）标记 signal_type=routine
- 用户陈述的习惯（喜欢/不喜欢/习惯）标记 signal_type=habit 或 preference
- 带时间/日期的计划标记 signal_type=schedule
- 无任何可提取内容时返回空数组 []

对话：
User: {user_message}
Assistant: {reply}

只输出有效 JSON，不用 markdown 标记："""


# ---------------------------------------------------------------------------
# UnifiedMemoryExtractor
# ---------------------------------------------------------------------------

class UnifiedMemoryExtractor:
    """Unified memory extractor: single LLM call for all memory signal types.

    Replaces MemoryExtractor + StewardSignalExtractor with one extraction.
    Outputs normalized MemoryProposal items via ProposalNormalizer.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        self._llm_client: LLMClient = llm_client
        self._normalizer: ProposalNormalizer = ProposalNormalizer()

    def extract(
        self,
        user_message: str,
        reply: str,
        trace_id: str | None = None,
        thread_id: str = "",
    ) -> list[MemoryProposal]:
        """Extract memories from a single conversation turn.

        Args:
            user_message: User's original message.
            reply: Assistant's reply.
            trace_id: Trace ID for logging.
            thread_id: Thread ID for scope inference.

        Returns:
            List of normalized MemoryProposal.
        """
        prompt = UNIFIED_EXTRACT_PROMPT.format(
            user_message=user_message, reply=reply
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

        try:
            response: LLMResponse = self._llm_client.chat(
                messages, trace_id=trace_id, temperature=0.1
            )
        except Exception:
            logger.exception("Unified extraction LLM call failed: trace_id=%s", trace_id)
            return []

        extracted: list[ExtractedMemory] = self._parse(response.content)
        return self._normalize_all(extracted, thread_id, trace_id)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse(self, raw: str) -> list[ExtractedMemory]:
        """Parse LLM raw output into ExtractedMemory list."""
        try:
            text = raw.strip()
            if not text:
                return []
            for fence in ("```json", "```"):
                if text.startswith(fence):
                    text = text[len(fence):].strip()
                if text.endswith("```"):
                    text = text[:-3].strip()
            if not text:
                return []
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                repaired = repair_json(text)
                data = json.loads(repaired)
            if not isinstance(data, list):
                return []
            results: list[ExtractedMemory] = []
            for item in data:
                try:
                    results.append(ExtractedMemory(
                        content=item.get("content", ""),
                        memory_type=item.get("memory_type", ""),
                        memory_key=item.get("memory_key", ""),
                        confidence=float(item.get("confidence", 0.5)),
                        source_span=item.get("source_span", ""),
                        durable=item.get("durable", True),
                        importance=float(item.get("importance", 0.5)),
                        signal_type=item.get("signal_type", ""),
                    ))
                except Exception:
                    logger.warning("Single extraction parse failed", exc_info=True)
                    continue
            return results
        except (json.JSONDecodeError, ValueError):
            logger.warning("Extraction JSON parse failed", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def _normalize_all(
        self,
        extracted: list[ExtractedMemory],
        thread_id: str,
        trace_id: str | None = None,
    ) -> list[MemoryProposal]:
        """Normalize all extracted items through ProposalNormalizer.

        Each proposal receives a unique idempotency key based on
        trace_id + proposal_index to prevent batch collisions.
        """
        source_event_ids: list[str] = [trace_id] if trace_id else []
        results: list[MemoryProposal] = []
        for i, em in enumerate(extracted):
            # Build evidence
            evidence: list[dict[str, Any]] = []
            if em.source_span:
                evidence.append({
                    "source_type": "user_message",
                    "trust_level": TrustLevel.TRUSTED.value,
                    "relation": "supports",
                    "content_span": em.source_span,
                })

            result: NormalizationResult = self._normalizer.normalize(
                content=em.content,
                memory_type_hint=em.memory_type or None,
                memory_key_hint=em.memory_key or None,
                confidence=em.confidence,
                importance=em.importance,
                trust_level=TrustLevel.TRUSTED.value,
                evidence=evidence,
                source_event_ids=source_event_ids,
                extractor_name="UnifiedMemoryExtractor",
                extractor_version="1.0",
                thread_id=thread_id,
            )
            if result.proposal is not None:
                # Override idempotency with unique index per batch item
                result.proposal.compute_request_idempotency(proposal_index=i)
                results.append(result.proposal)
            else:
                logger.debug(
                    "Proposal normalization failed: %s", result.error
                )

        return results


