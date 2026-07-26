"""记忆写入权威与读取敏感度的集中确定性策略。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from aiive.memory.memory_types import (
    AUTHORITY_RULES,
    EvidenceSourceType,
    Sensitivity,
)


class MemoryReadChannel(StrEnum):
    """记忆内容的主要读取通道。"""

    LLM_CONTEXT = "llm_context"
    API = "api"
    TOOL = "tool"


class MemoryPolicyReason(StrEnum):
    """稳定的策略决策原因码。"""

    AUTHORITY_ALLOWED = "authority_allowed"
    AUTHORITY_SOURCE_UNKNOWN = "authority_source_unknown"
    AUTHORITY_SOURCE_FORBIDDEN = "authority_source_forbidden"
    READ_ALLOWED = "read_allowed"
    READ_LEGACY_DEFAULT_NORMAL = "read_legacy_default_normal"
    READ_SECRET_BLOCKED = "read_secret_blocked"
    READ_SECRET_REDACTED = "read_secret_redacted"
    READ_SENSITIVITY_UNKNOWN_BLOCKED = "read_sensitivity_unknown_blocked"
    READ_SENSITIVITY_UNKNOWN_REDACTED = "read_sensitivity_unknown_redacted"


@dataclass(frozen=True)
class MemoryPolicyDecision:
    """纯确定性的策略结果。"""

    allowed: bool
    reason_code: str
    redacted: bool = False


# 参与写入权威判定的 evidence relation。relation 为 "derived_from" 的证据
# 仅作 provenance（出处追溯），不参与权威判定 —— 例如 assistant 回复事件
# （llm_derivation）作为提取上下文的出处，不应因其不在 AUTHORITY_RULES 中
# 而导致整个提案被拒。
AUTHORITY_RELATIONS: frozenset[str] = frozenset({"supports", "confirms", "corrects"})


_LEGACY_EVIDENCE_SOURCE_MAP: dict[str, str] = {
    "user_message": EvidenceSourceType.USER_ASSERTION.value,
    "user_command": EvidenceSourceType.USER_ASSERTION.value,
    "llm_reply": EvidenceSourceType.LLM_DERIVATION.value,
    "assistant_message": EvidenceSourceType.LLM_DERIVATION.value,
    "webpage": EvidenceSourceType.EXTERNAL_CLAIM.value,
    "pdf": EvidenceSourceType.EXTERNAL_CLAIM.value,
    "email": EvidenceSourceType.EXTERNAL_CLAIM.value,
    "code_comment": EvidenceSourceType.EXTERNAL_CLAIM.value,
    "retrieved_knowledge": EvidenceSourceType.EXTERNAL_CLAIM.value,
    "mcp_description": EvidenceSourceType.EXTERNAL_CLAIM.value,
    "maintenance": EvidenceSourceType.KERNEL_RESULT.value,
}


class MemoryPolicyEngine:
    """统一执行 evidence taxonomy、写入权威与读取敏感度策略。"""

    REDACTED_CONTENT = "[记忆内容已脱敏]"

    @staticmethod
    def normalize_evidence_source(source_type: str) -> str | None:
        """将旧来源名称映射为统一 taxonomy；未知来源返回 None。"""
        canonical = {item.value for item in EvidenceSourceType}
        if source_type in canonical:
            return source_type
        return _LEGACY_EVIDENCE_SOURCE_MAP.get(source_type)

    def decide_authority(self, memory_type: str, source_types: list[str]) -> MemoryPolicyDecision:
        """要求每条 evidence 都对目标记忆类型具有写入权威。

        调用方应只传入 relation ∈ AUTHORITY_RELATIONS 的证据来源
        （provenance-only 的 derived_from 证据不参与权威判定）；
        过滤后为空列表 → 无任何权威证据 → 拒绝。
        """
        normalized = [self.normalize_evidence_source(source) for source in source_types]
        if any(source is None for source in normalized):
            return MemoryPolicyDecision(False, MemoryPolicyReason.AUTHORITY_SOURCE_UNKNOWN.value)
        allowed_sources = AUTHORITY_RULES.get(memory_type, frozenset())
        if normalized and all(source in allowed_sources for source in normalized):
            return MemoryPolicyDecision(True, MemoryPolicyReason.AUTHORITY_ALLOWED.value)
        return MemoryPolicyDecision(False, MemoryPolicyReason.AUTHORITY_SOURCE_FORBIDDEN.value)

    @staticmethod
    def resolve_sensitivity(value: str | None) -> Sensitivity | None:
        """解析敏感度；非法非空值返回 None。"""
        try:
            return Sensitivity(value) if value is not None else Sensitivity.NORMAL
        except ValueError:
            return None

    def decide_read(self, sensitivity: str | None, channel: MemoryReadChannel) -> MemoryPolicyDecision:
        """对主要读取通道作集中决策，旧 NULL 兼容为 normal。"""
        if sensitivity is None:
            return MemoryPolicyDecision(True, MemoryPolicyReason.READ_LEGACY_DEFAULT_NORMAL.value)
        resolved = self.resolve_sensitivity(sensitivity)
        if resolved is None:
            code = (
                MemoryPolicyReason.READ_SENSITIVITY_UNKNOWN_BLOCKED
                if channel == MemoryReadChannel.LLM_CONTEXT
                else MemoryPolicyReason.READ_SENSITIVITY_UNKNOWN_REDACTED
            )
            return MemoryPolicyDecision(False, code.value, redacted=channel != MemoryReadChannel.LLM_CONTEXT)
        if resolved == Sensitivity.SECRET:
            code = (
                MemoryPolicyReason.READ_SECRET_BLOCKED
                if channel == MemoryReadChannel.LLM_CONTEXT
                else MemoryPolicyReason.READ_SECRET_REDACTED
            )
            return MemoryPolicyDecision(False, code.value, redacted=channel != MemoryReadChannel.LLM_CONTEXT)
        return MemoryPolicyDecision(True, MemoryPolicyReason.READ_ALLOWED.value)

    def render_content(self, content: str, sensitivity: str | None, channel: MemoryReadChannel) -> str | None:
        """按读取决策返回原文、脱敏占位或 None。"""
        decision = self.decide_read(sensitivity, channel)
        if decision.allowed:
            return content
        if decision.redacted:
            return self.REDACTED_CONTENT
        return None
