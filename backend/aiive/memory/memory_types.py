"""Memory Types: canonical type system for the memory subsystem.

Defines all enums, models, and constants for the unified memory architecture.
Single source of truth for MemoryType, ScopeType, lifecycle states, validity states,
lineage operations, trust levels, stability, and MemoryProposal.

Design principles:
- 8 canonical MemoryTypes; old types mapped via LEGACY_TYPE_MAP.
- lifecycle_state, validity_state, lineage_operation are three orthogonal axes.
- MemoryProposal is the unified input model from all sources (extractor, tool, maintenance).
"""

from __future__ import annotations

import hashlib
import uuid
from enum import StrEnum

from pydantic import BaseModel, Field


# ============================================================================
# Canonical MemoryType (8 types)
# ============================================================================

class MemoryType(StrEnum):
    """Canonical memory types — the only valid values for memory_records.memory_type."""
    USER_PROFILE = "user_profile"
    PROJECT = "project"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"
    AGENT_SELF = "agent_self"
    POLICY = "policy"
    ENVIRONMENT = "environment"
    KNOWLEDGE = "knowledge"


CANONICAL_TYPES: frozenset[str] = frozenset(t.value for t in MemoryType)


# Memory key guide (single source of truth for key naming convention)
# Used by extraction prompts and tool descriptions.
MEMORY_KEY_GUIDE: str = """- agent.display_name: Agent 可见名称。触发："以后你叫X" → content="X"（纯值）
- user.display_name: 用户称呼。触发："以后叫我X" → content="X"（纯值）
- user.name: 用户真实姓名，仅当用户明确说"我的真实姓名是X"。content="X"（纯值）
- user.preference.response_style: 回复风格偏好。触发："我喜欢你回答简洁一点"
- agent.persona.tone: Agent 语气/人格。触发："你以后说话活泼一点"
- agent.persona.relationship: 主从/关系风格（"你是我的主人""我是你的上司"）
- user.preference.<topic>: 用户某主题偏好
- user.routine.<desc>: 用户日常规律
- user.habit.<desc>: 用户习惯
- user.schedule.<desc>: 用户日程
- project.<project_name>.<topic>: 项目决策。触发："AIive 后端用 FastAPI" → project.aiive.backend_stack
- policy.<topic>: 规则与策略约束
身份键的 content 必须是纯值，不含前缀或关系表述。"""


# Legacy → Canonical mapping.  Values that cannot be resolved become None
# and the record is marked legacy_unresolved during migration.
LEGACY_TYPE_MAP: dict[str, str | None] = {
    "user_profile": MemoryType.USER_PROFILE.value,
    "agent_self": MemoryType.AGENT_SELF.value,
    "project": MemoryType.PROJECT.value,
    "policy": MemoryType.POLICY.value,
    "environment": MemoryType.ENVIRONMENT.value,
    "fact": MemoryType.KNOWLEDGE.value,  # default for generic facts
    "preference": MemoryType.USER_PROFILE.value,  # demoted to memory_key
    "routine": MemoryType.USER_PROFILE.value,      # demoted to memory_key
    "habit": MemoryType.USER_PROFILE.value,         # demoted to memory_key
    "schedule": MemoryType.USER_PROFILE.value,      # demoted to memory_key
    "name": MemoryType.USER_PROFILE.value,          # demoted to memory_key
    "episodic": MemoryType.EPISODIC.value,
    "procedural": MemoryType.PROCEDURAL.value,
    "knowledge": MemoryType.KNOWLEDGE.value,
}

# Valid memory_key prefix mappings for legacy type → canonical_key inference
LEGACY_TYPE_TO_KEY_PREFIX: dict[str, str] = {
    "routine": "user.routine.",
    "habit": "user.habit.",
    "schedule": "user.schedule.",
    "preference": "user.preference.",
    "name": "user.",
}


# ============================================================================
# Scope
# ============================================================================

class ScopeType(StrEnum):
    """Scope types. global requires scope_id=NULL; others require non-NULL."""
    GLOBAL = "global"
    PROJECT = "project"
    THREAD = "thread"
    WORKSPACE = "workspace"
    CAPABILITY = "capability"
    ENVIRONMENT = "environment"


SCOPES_REQUIRING_ID: frozenset[str] = frozenset({
    ScopeType.PROJECT.value,
    ScopeType.THREAD.value,
    ScopeType.WORKSPACE.value,
    ScopeType.CAPABILITY.value,
    ScopeType.ENVIRONMENT.value,
})


def validate_scope(scope_type: str, scope_id: str | None) -> None:
    """Raise ValueError if scope_type/scope_id combination is invalid."""
    if scope_type not in {t.value for t in ScopeType}:
        raise ValueError(f"Unknown scope_type: {scope_type}")
    if scope_type == ScopeType.GLOBAL.value:
        if scope_id is not None and scope_id != "":
            raise ValueError("scope_id must be NULL/empty for global scope")
    else:
        if not scope_id:
            raise ValueError(f"scope_id is required for scope_type={scope_type}")


# ============================================================================
# Lifecycle / Validity / Lineage states (three orthogonal axes)
# ============================================================================

class LifecycleState(StrEnum):
    """MemoryRecord lifecycle: candidate → active → sleeping → archived → forgotten.

    candidate cannot transition directly to sleeping.
    """
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SLEEPING = "sleeping"
    ARCHIVED = "archived"
    FORGOTTEN = "forgotten"


class ValidityState(StrEnum):
    """Data validity within the lifecycle."""
    VALID = "valid"
    SUPERSEDED = "superseded"
    CONTRADICTED = "contradicted"
    EXPIRED = "expired"


class LineageOperation(StrEnum):
    """Lineage / event operation types.

    reject / ignore / discard belong to proposal-level decisions,
    NOT to MemoryRecord lifecycle.
    """
    CREATE = "create"
    REINFORCE = "reinforce"
    REVISE = "revise"
    SUPERSEDE = "supersede"
    MERGE = "merge"
    PROMOTE = "promote"       # candidate → active
    SLEEP = "sleep"
    WAKE = "wake"
    ARCHIVE = "archive"
    FORGET = "forget"


# ============================================================================
# Trust & Stability enums
# ============================================================================

class TrustLevel(StrEnum):
    """Evidence / record trust level.

    No default; every proposal must explicitly assign per-evidence trust_level.
    """
    TRUSTED = "trusted"
    SEMI_TRUSTED = "semi_trusted"
    UNTRUSTED = "untrusted"
    UNTRUSTED_DERIVED = "untrusted_derived"


class Stability(StrEnum):
    """How stable a memory is expected to be over time."""
    STABLE = "stable"
    CONTEXTUAL = "contextual"
    VOLATILE = "volatile"


class EvidenceSourceType(StrEnum):
    """Evidence source classification. Used for authority rules."""
    USER_ASSERTION = "user_assertion"
    TOOL_OBSERVATION = "tool_observation"
    TEST_RESULT = "test_result"
    KERNEL_RESULT = "kernel_result"
    EXTERNAL_CLAIM = "external_claim"
    LLM_DERIVATION = "llm_derivation"


class Sensitivity(StrEnum):
    """Data sensitivity for model boundary enforcement.
    
    Actual redaction/cloud routing is handled by Model Boundary module.
    """
    NORMAL = "normal"
    PERSONAL = "personal"
    CONFIDENTIAL = "confidential"
    SECRET = "secret"


# Per-type allowed evidence sources.
# agent_self: accepts test/kernel/tool/selfdev evidence, not only user.
# environment: accepts tool_observation.
# policy memory is user/project policy only; Kernel Contract is NOT MemoryRecord.
AUTHORITY_RULES: dict[str, frozenset[str]] = {
    MemoryType.USER_PROFILE.value: frozenset({
        EvidenceSourceType.USER_ASSERTION.value,
    }),
    MemoryType.AGENT_SELF.value: frozenset({
        EvidenceSourceType.USER_ASSERTION.value,
        EvidenceSourceType.TEST_RESULT.value,
        EvidenceSourceType.KERNEL_RESULT.value,
        EvidenceSourceType.TOOL_OBSERVATION.value,
    }),
    MemoryType.POLICY.value: frozenset({
        EvidenceSourceType.USER_ASSERTION.value,
    }),
    MemoryType.ENVIRONMENT.value: frozenset({
        EvidenceSourceType.USER_ASSERTION.value,
        EvidenceSourceType.TOOL_OBSERVATION.value,
        EvidenceSourceType.KERNEL_RESULT.value,
    }),
    MemoryType.PROJECT.value: frozenset({
        EvidenceSourceType.USER_ASSERTION.value,
        EvidenceSourceType.TOOL_OBSERVATION.value,
        EvidenceSourceType.TEST_RESULT.value,
    }),
    MemoryType.KNOWLEDGE.value: frozenset({
        EvidenceSourceType.USER_ASSERTION.value,
        EvidenceSourceType.TOOL_OBSERVATION.value,
        EvidenceSourceType.EXTERNAL_CLAIM.value,
        EvidenceSourceType.TEST_RESULT.value,
    }),
    MemoryType.EPISODIC.value: frozenset({
        EvidenceSourceType.USER_ASSERTION.value,
        EvidenceSourceType.TOOL_OBSERVATION.value,
    }),
    MemoryType.PROCEDURAL.value: frozenset({
        EvidenceSourceType.USER_ASSERTION.value,
        EvidenceSourceType.TOOL_OBSERVATION.value,
        EvidenceSourceType.TEST_RESULT.value,
    }),
}
# Default for types not in AUTHORITY_RULES: allow user_assertion only
_DEFAULT_AUTHORITY: frozenset[str] = frozenset({
    EvidenceSourceType.USER_ASSERTION.value,
})


def is_evidence_authorized(memory_type: str, source_type: str) -> bool:
    """Check if evidence source_type is authorized for the given memory_type."""
    allowed = AUTHORITY_RULES.get(memory_type, _DEFAULT_AUTHORITY)
    return source_type in allowed


# ============================================================================
# Evidence model
# ============================================================================

class EvidenceItem(BaseModel):
    """A single piece of evidence backing a MemoryProposal."""
    source_event_id: str | None = None
    source_type: str  # EvidenceSourceType value
    trust_level: str  # TrustLevel value
    relation: str = "supports"  # supports / contradicts / confirms / corrects / derived_from
    content_span: str | None = None


# ============================================================================
# MemoryProposal — unified input model
# ============================================================================

class MemoryProposal(BaseModel):
    """Unified memory proposal from any source (extractor, tool, maintenance).

    This is the ONLY input format accepted by MemoryWriteService.
    Old ExtractedMemory and steward signal dicts must be normalized into this model.
    """

    proposal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_event_ids: list[str] = Field(default_factory=list)

    # --- target ---
    memory_type: str                     # canonical MemoryType value
    canonical_key: str                   # resolved canonical_key from MemoryKeyRegistry
    scope_type: str = ScopeType.GLOBAL.value
    scope_id: str | None = None

    content: str = ""
    structured_value: dict[str, object] | None = None

    # --- evidence ---
    evidence: list[EvidenceItem] = Field(default_factory=list)
    trust_level: str = TrustLevel.SEMI_TRUSTED.value

    # --- scores ---
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    stability: str = Stability.CONTEXTUAL.value
    stability_score: float = Field(default=0.5, ge=0.0, le=1.0)

    # --- proposed operation (extractor hint; final decision by Gate + Resolver) ---
    proposed_operation: str = "create"

    # --- extractor metadata ---
    extractor_name: str = ""
    extractor_version: str = ""

    # --- 幂等与去重 ---
    # 请求幂等键: 防止同一 source_event 被重复消费
    # sha256(source_event_ids | extractor_name | extractor_version | proposal_index)
    request_idempotency_key: str = ""
    # 旧字段，兼容迁移期，等同于 request_idempotency_key
    idempotency_key: str = ""

    # --- 语义去重 hash（由 ConflictResolver 使用）---
    # content_dedup_hash: sha256(normalized_content)
    # 不同于 request_idempotency: 不同 source_event 的相同内容可以 reinforce
    content_hash: str = ""

    # --- proposal-level fields (not stored on MemoryRecord) ---
    requires_confirmation: bool = False
    raw_payload: dict[str, object] | None = None
    normalized_payload: dict[str, object] | None = None

    def compute_request_idempotency(self, proposal_index: int = 0) -> str:
        """计算请求幂等键: 防止同一事件重复处理。
        
        格式: sha256(source_event_ids | extractor_name | extractor_version | index)
        """
        components = [
            "|".join(sorted(self.source_event_ids)),
            self.extractor_name,
            self.extractor_version,
            str(proposal_index),
        ]
        raw = "|".join(components)
        key = f"req:{hashlib.sha256(raw.encode()).hexdigest()[:24]}"
        self.request_idempotency_key = key
        self.idempotency_key = key
        return key

    def compute_content_hash(self) -> str:
        """计算语义去重 hash: sha256(normalized_content)"""
        self.content_hash = hashlib.sha256(self.content.encode()).hexdigest()[:16]
        return self.content_hash


# ============================================================================
# Canonical key helpers
# ============================================================================

def infer_canonical_key(
    memory_type_hint: str | None,
    memory_key: str | None,
    content: str = "",
) -> str | None:
    """Best-effort inference of canonical_key from legacy hints.

    Used during migration and for backward-compatible normalization.
    """
    if memory_key and memory_key.strip():
        return memory_key.strip()

    if memory_type_hint:
        prefix = LEGACY_TYPE_TO_KEY_PREFIX.get(memory_type_hint)
        if prefix and content:
            slug = content.lower().replace(" ", "_")[:40]
            return f"{prefix}{slug}"

    return None


def is_canonical_type(value: str) -> bool:
    """Return True if value is a valid canonical MemoryType."""
    return value in CANONICAL_TYPES
