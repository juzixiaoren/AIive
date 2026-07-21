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

from aiive.core.text_utils import strip_code_fence

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
        source_event_ids: list[str] | None = None,
        assistant_event_ids: list[str] | None = None,
    ) -> list[MemoryProposal]:
        """Extract memories from a single conversation turn.

        Args:
            user_message: User's original message.
            reply: Assistant's reply.
            trace_id: Trace ID for logging.
            thread_id: Thread ID for scope inference.
            source_event_ids: 当前 Turn 中已持久化的真实 Event.id 列表，
                作为记忆的精确 provenance。未提供时退回 ``[trace_id]``，
                此兜底场景下不会把 trace_id 作为 evidence 的 source_event_id
                落库（仅保留 span 证据），避免污染 provenance。
            assistant_event_ids: 属于 assistant 回复事件的 Event.id 列表，
                其中的事件会被标记为 llm_reply 而非 user_message，使 provenance
                语义更精确。仅供已确定真实 llm_response Event.id 的同步抽取路径使用。

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
        return self._normalize_all(
            extracted, thread_id, trace_id,
            source_event_ids=source_event_ids,
            assistant_event_ids=assistant_event_ids,
        )

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse(self, raw: str) -> list[ExtractedMemory]:
        """Parse LLM raw output into ExtractedMemory list."""
        try:
            text = strip_code_fence(raw)
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
        source_event_ids: list[str] | None = None,
        assistant_event_ids: list[str] | None = None,
    ) -> list[MemoryProposal]:
        """Normalize all extracted items through ProposalNormalizer.

        Each proposal receives a unique idempotency key based on
        source_event_ids + proposal_index to prevent batch collisions.

        证据构建规则：span 与真实事件 id 互补。当调用方提供真实
        ``source_event_ids`` 时，逐事件建 evidence（``assistant_event_ids``
        中的事件标为 ``llm_reply``，其余为 ``user_message``），并把
        ``source_span`` 附到首个 user_message 证据项；未提供真实事件时退回
        ``[trace_id]`` 且仅保留 span 证据（不落库伪 source_event_id）。
        """
        # 区分调用方提供的真实 Event.id 与 trace_id 兜底：只有真实事件才允许
        # 作为 evidence 的 source_event_id 落库（trace_id 不是真实 Event，
        # 落库会污染 provenance 且无法与 Event 表 JOIN）。
        has_real_events = bool(source_event_ids)
        if not source_event_ids:
            source_event_ids = [trace_id] if trace_id else []
        assistant_set = set(assistant_event_ids or [])
        results: list[MemoryProposal] = []
        for i, em in enumerate(extracted):
            # 构建 evidence：span 与真实事件 id 互补而非互斥。
            # - 有真实事件：逐事件建 evidence（user→user_message、assistant→llm_reply），
            #   并把 source_span 附到首个 user_message 证据项，保证 span 与事件关联。
            # - 无真实事件（trace_id 兜底）：退回仅含 span 的旧行为，不写入伪 source_event_id。
            evidence: list[dict[str, Any]] = []
            if has_real_events:
                span_attached = False
                for seid in source_event_ids:
                    is_assistant = seid in assistant_set
                    item: dict[str, Any] = {
                        "source_event_id": seid,
                        "source_type": "llm_reply" if is_assistant else "user_message",
                        "trust_level": TrustLevel.TRUSTED.value,
                        "relation": "supports",
                    }
                    if em.source_span and not is_assistant and not span_attached:
                        item["content_span"] = em.source_span
                        span_attached = True
                    evidence.append(item)
                # 无 user_message 事件承载 span 时，追加独立 span 证据项兜底。
                if em.source_span and not span_attached:
                    evidence.append({
                        "source_type": "user_message",
                        "trust_level": TrustLevel.TRUSTED.value,
                        "relation": "supports",
                        "content_span": em.source_span,
                    })
            elif em.source_span:
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
                assistant_event_ids=assistant_event_ids,
                extractor_name="UnifiedMemoryExtractor",
                extractor_version="1.0",
                thread_id=thread_id,
            )
            if result.proposal is not None:
                # Phase 2: durable + execution_mode
                result.proposal.durable = em.durable
                result.proposal.execution_mode = "system_best_effort"
                # Override idempotency with unique index per batch item
                result.proposal.compute_request_idempotency(proposal_index=i)
                results.append(result.proposal)
            else:
                logger.debug(
                    "Proposal normalization failed: %s", result.error
                )

        return results


