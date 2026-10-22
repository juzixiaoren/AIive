"""MemoryPolicyEngine 写入权威与敏感度读取策略回归测试。"""
from __future__ import annotations

from datetime import datetime, timezone

from aiive.db.models import MemoryRecord
from aiive.memory.automatic_recall import AutomaticRecallEngine
from aiive.memory.memory_gate import MemoryGate
from aiive.memory.memory_policy import (
    MemoryPolicyEngine,
    MemoryPolicyReason,
    MemoryReadChannel,
)
from aiive.memory.memory_types import EvidenceItem, MemoryProposal, TrustLevel
from aiive.memory.recall_config import RecallConfig
from aiive.memory.recall_models import MemoryRecallRequest, ScopeContext


def _proposal(memory_type: str, source_type: str) -> MemoryProposal:
    """构造带单一 evidence 的高置信提案。"""
    return MemoryProposal(
        memory_type=memory_type,
        canonical_key=f"test.{memory_type}",
        content="测试内容",
        confidence=0.9,
        evidence=[EvidenceItem(
            source_type=source_type,
            trust_level=TrustLevel.TRUSTED.value,
        )],
    )


def _record(db, *, record_id: str, content: str, sensitivity: str | None) -> MemoryRecord:
    """构造可被词汇召回的 active 记录。"""
    now = datetime.now(timezone.utc)
    record = MemoryRecord(
        id=record_id,
        memory_type="knowledge",
        canonical_key=f"knowledge.{record_id}",
        scope_type="global",
        content=content,
        lifecycle_state="active",
        validity_state="valid",
        sensitivity=sensitivity,
        confidence=0.9,
        importance=0.8,
        observed_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(record)
    db.flush()
    return record


def test_legacy_evidence_sources_normalize_to_unified_taxonomy():
    policy = MemoryPolicyEngine()
    assert policy.normalize_evidence_source("user_message") == "user_assertion"
    assert policy.normalize_evidence_source("llm_reply") == "llm_derivation"
    assert policy.normalize_evidence_source("webpage") == "external_claim"


def test_unknown_evidence_source_is_rejected_with_reason_code():
    decision = MemoryPolicyEngine().decide_authority("knowledge", ["mystery_source"])
    assert decision.allowed is False
    assert decision.reason_code == MemoryPolicyReason.AUTHORITY_SOURCE_UNKNOWN.value


def test_authority_matrix_rejects_external_user_profile():
    decision = MemoryPolicyEngine().decide_authority("user_profile", ["webpage"])
    assert decision.allowed is False
    assert decision.reason_code == MemoryPolicyReason.AUTHORITY_SOURCE_FORBIDDEN.value
    gate = MemoryGate().decide(_proposal("user_profile", "webpage"))
    assert gate.decision == "reject"
    assert gate.blocked_reason == MemoryPolicyReason.AUTHORITY_SOURCE_FORBIDDEN.value


def test_mixed_evidence_requires_every_source_to_be_authorized():
    proposal = _proposal("user_profile", "user_message")
    proposal.evidence.append(EvidenceItem(
        source_type="webpage",
        trust_level=TrustLevel.UNTRUSTED.value,
    ))

    decision = MemoryGate().decide(proposal)

    assert decision.decision == "reject"
    assert decision.blocked_reason == MemoryPolicyReason.AUTHORITY_SOURCE_FORBIDDEN.value


def test_canonical_user_assertion_is_accepted_by_unified_gate():
    decision = MemoryGate().decide(_proposal("user_profile", "user_assertion"))
    assert decision.decision == "active"


def test_dual_event_evidence_user_profile_passes_gate():
    """端到端回归：user_message + llm_reply 双事件 evidence 的 user_profile 提案应通过 gate。

    修复前：assistant 事件 evidence 的 llm_reply 归一化为 llm_derivation，而任何
    类型的 AUTHORITY_RULES 都不含 llm_derivation → 自动提取（双事件 provenance）
    的提案 100% 被 Gate 拒绝。修复后 assistant 证据 relation=derived_from，
    仅作 provenance、不参与权威判定。
    """
    import json

    from aiive.core.llm_client import FakeLLMClient
    from aiive.memory.memory_extractor import UnifiedMemoryExtractor

    fixed = json.dumps([{
        "content": "橘子",
        "memory_type": "user_profile",
        "memory_key": "user.display_name",
        "confidence": 0.95,
        "importance": 0.8,
        "source_span": "以后叫我橘子",
        "durable": True,
    }])
    extractor = UnifiedMemoryExtractor(FakeLLMClient(fixed_content=fixed))
    proposals = extractor.extract(
        user_message="以后叫我橘子",
        reply="好的，橘子！",
        trace_id="trace-gate",
        thread_id="thread-gate",
        source_event_ids=["ev_user", "ev_assistant"],
        assistant_event_ids=["ev_assistant"],
    )
    assert len(proposals) == 1
    proposal = proposals[0]
    by_id = {e.source_event_id: e for e in proposal.evidence}
    assert by_id["ev_user"].source_type == "user_message"
    assert by_id["ev_user"].relation == "supports"
    assert by_id["ev_assistant"].source_type == "llm_reply"
    assert by_id["ev_assistant"].relation == "derived_from"

    decision = MemoryGate().decide(proposal)
    assert decision.decision == "active", f"gate rejected: {decision.reason}"


def test_all_derived_from_evidence_is_still_rejected():
    """全部证据均为 provenance-only（derived_from）→ 无任何权威证据 → 维持拒绝。"""
    proposal = _proposal("user_profile", "llm_reply")
    proposal.evidence[0].relation = "derived_from"
    decision = MemoryGate().decide(proposal)
    assert decision.decision == "reject"


def test_legacy_null_sensitivity_is_read_as_normal():
    policy = MemoryPolicyEngine()
    decision = policy.decide_read(None, MemoryReadChannel.LLM_CONTEXT)
    assert decision.allowed is True
    assert decision.reason_code == MemoryPolicyReason.READ_LEGACY_DEFAULT_NORMAL.value
    assert policy.render_content("旧记忆", None, MemoryReadChannel.LLM_CONTEXT) == "旧记忆"


def test_secret_is_blocked_for_llm_and_redacted_for_api():
    policy = MemoryPolicyEngine()
    assert policy.render_content("密钥", "secret", MemoryReadChannel.LLM_CONTEXT) is None
    assert policy.render_content("密钥", "secret", MemoryReadChannel.API) == policy.REDACTED_CONTENT
    assert policy.render_content("密钥", "secret", MemoryReadChannel.TOOL) == policy.REDACTED_CONTENT


def test_invalid_sensitivity_is_fail_closed_before_persistence():
    """非法非空敏感度即使来自旁路也不得返回原文。"""
    policy = MemoryPolicyEngine()
    assert policy.render_content("异常内容", "mystery", MemoryReadChannel.LLM_CONTEXT) is None
    assert policy.render_content("异常内容", "mystery", MemoryReadChannel.API) == policy.REDACTED_CONTENT


def test_automatic_recall_filters_secret_without_crashing(db):
    normal = _record(db, record_id="normal-memory", content="量子策略普通内容", sensitivity=None)
    secret = _record(db, record_id="secret-memory", content="量子策略秘密内容", sensitivity="secret")
    db.commit()

    pack, _ = AutomaticRecallEngine(db, RecallConfig()).recall(
        MemoryRecallRequest(query="量子策略", scope_context=ScopeContext()),
    )

    ids = {item.memory_id for item in pack.items}
    assert normal.id in ids
    assert secret.id not in ids
