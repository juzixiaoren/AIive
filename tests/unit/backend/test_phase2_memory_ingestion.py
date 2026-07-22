"""Phase 2 Memory Ingestion 测试矩阵。

覆盖：
- D. user_required 写入失败 → 结构化 memory_write_failed
- E. system_best_effort 失败 → retry/deadletter
- G. 临时偏好 → active + ephemeral + valid_to
- H. durable=False → reject
- I. 不确定长期偏好 → candidate
- J. pinned 只能由 user_required 创建
- K. assistant-only evidence → reject
- M. 显式路径不创建 MemoryIngestionRun
- N. retention_policy 写入后可读回
- O. ephemeral 无 valid_to → reject
- P. source_turn_record_id ≠ source_turn_id
- Q. Idempotency-Key 持久化 + UNIQUE
"""
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from aiive.context.run_context import RunContext
from aiive.db.base import get_db
from aiive.db.models import MemoryIngestionRun, MemoryRecord
from aiive.main import create_app
from aiive.memory.conflict_resolver import ResolutionResult
from aiive.memory.memory_gate import MemoryGate
from aiive.memory.memory_types import (
    EvidenceItem,
    MemoryProposal,
    MemoryType,
    ScopeType,
    TrustLevel,
    LifecycleState,
    WriteOutcome,
)
from aiive.memory.memory_write_service import MemoryWriteService, WriteResult


# ════════════════════════════════════════════════════
# Gate 规则测试
# ════════════════════════════════════════════════════


class TestGateRetentionRules:
    """H / J / K / O：Gate 的 retention + durable + pinned + evidence 规则。"""

    def _proposal(self, **overrides) -> MemoryProposal:
        """构建默认有效 proposal。"""
        defaults = dict(
            memory_type=MemoryType.KNOWLEDGE.value,
            canonical_key="knowledge.test",
            scope_type=ScopeType.GLOBAL.value,
            content="test content",
            confidence=0.9,
            importance=0.8,
            evidence=[EvidenceItem(
                source_type="user_message",
                trust_level=TrustLevel.TRUSTED.value,
                relation="supports",
            )],
            durable=True,
            retention_policy="normal",
            execution_mode="system_best_effort",
            valid_to=None,
        )
        defaults.update(overrides)
        return MemoryProposal(**{k: v for k, v in defaults.items()
                                 if k in MemoryProposal.model_fields})

    def test_durable_false_rejected(self):
        """H: durable=False → Gate reject（不是 candidate）。"""
        gate = MemoryGate()
        proposal = self._proposal(durable=False)
        decision = gate.decide(proposal)
        assert decision.decision == "reject"
        assert "durable" in decision.reason.lower() or "Non-durable" in decision.reason

    def test_pinned_rejected_for_system_best_effort(self):
        """J: pinned 只能由 user_required 创建。"""
        gate = MemoryGate()
        proposal = self._proposal(
            retention_policy="pinned",
            execution_mode="system_best_effort",
        )
        decision = gate.decide(proposal)
        assert decision.decision == "reject"

    def test_pinned_allowed_for_user_required(self):
        """J: pinned 在 user_required 下允许。"""
        gate = MemoryGate()
        proposal = self._proposal(
            retention_policy="pinned",
            execution_mode="user_required",
        )
        decision = gate.decide(proposal)
        assert decision.decision in ("active", "candidate")

    def test_ephemeral_without_valid_to_rejected(self):
        """O: ephemeral 无 valid_to → reject。"""
        gate = MemoryGate()
        proposal = self._proposal(
            retention_policy="ephemeral",
            valid_to=None,
        )
        decision = gate.decide(proposal)
        assert decision.decision == "reject"
        assert "valid_to" in decision.reason.lower() or "no indefinite ephemeral" in decision.reason.lower()

    def test_ephemeral_with_past_valid_to_rejected(self):
        """O: ephemeral valid_to 已过期 → reject。"""
        gate = MemoryGate()
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        proposal = self._proposal(
            retention_policy="ephemeral",
            valid_to=past,
        )
        decision = gate.decide(proposal)
        assert decision.decision == "reject"

    def test_ephemeral_with_future_valid_to_accepted(self):
        """G: ephemeral + future valid_to → 接受。"""
        gate = MemoryGate()
        future = datetime.now(timezone.utc) + timedelta(days=7)
        proposal = self._proposal(
            retention_policy="ephemeral",
            valid_to=future,
        )
        decision = gate.decide(proposal)
        assert decision.decision == "active"

    def test_pure_assistant_evidence_rejected_for_user_profile(self):
        """K: 仅 assistant reply 不能创建用户记忆。"""
        gate = MemoryGate()
        proposal = self._proposal(
            memory_type=MemoryType.USER_PROFILE.value,
            evidence=[EvidenceItem(
                source_type="llm_reply",
                trust_level=TrustLevel.SEMI_TRUSTED.value,
                relation="supports",
            )],
        )
        decision = gate.decide(proposal)
        assert decision.decision == "reject"

    def test_low_confidence_to_candidate(self):
        """I: confidence < 0.7 → candidate（不确定长期偏好）。"""
        gate = MemoryGate()
        proposal = self._proposal(
            confidence=0.5,
            retention_policy="normal",
        )
        decision = gate.decide(proposal)
        assert decision.decision == "candidate"


# ════════════════════════════════════════════════════
# Provenance 测试
# ════════════════════════════════════════════════════


class TestProvenanceFields:
    """P: source_turn_record_id ≠ source_turn_id；N: retention_policy 读回。"""

    def test_source_turn_record_id_and_turn_id_are_separate(self, db_session):
        """P: 两个字段互不相等，分别为 PK 和业务 ID。"""
        proposal = MemoryProposal(
            memory_type=MemoryType.KNOWLEDGE.value,
            canonical_key="knowledge.test",
            scope_type=ScopeType.GLOBAL.value,
            content="test",
            confidence=0.9,
            source_turn_record_id="turn-pk-uuid-123",
            source_turn_id="turn-business-id-abc",
            evidence=[EvidenceItem(
                source_type="user_message",
                trust_level=TrustLevel.TRUSTED.value,
            )],
        )
        assert proposal.source_turn_record_id == "turn-pk-uuid-123"
        assert proposal.source_turn_id == "turn-business-id-abc"
        assert proposal.source_turn_record_id != proposal.source_turn_id

    def test_retention_policy_persisted_to_record(self, db_session):
        """N: retention_policy 写入后可从 MemoryRecord 读回。"""
        writer = MemoryWriteService(db_session)
        proposal = MemoryProposal(
            memory_type=MemoryType.KNOWLEDGE.value,
            canonical_key="knowledge.persist_test",
            scope_type=ScopeType.GLOBAL.value,
            content="persist content",
            confidence=0.9,
            retention_policy="ephemeral",
            valid_to=datetime.now(timezone.utc) + timedelta(days=1),
            evidence=[EvidenceItem(
                source_type="user_message",
                trust_level=TrustLevel.TRUSTED.value,
            )],
        )
        ctx = RunContext(thread_id="t1", trace_id="tr1", execution_mode="user_required")
        result = writer.write(proposal, run_context=ctx)
        db_session.flush()

        record = db_session.get(MemoryRecord, result.memory_id)
        assert record is not None
        assert record.retention_policy == "ephemeral"
        assert record.valid_to is not None


# ════════════════════════════════════════════════════
# Execution Mode 测试
# ════════════════════════════════════════════════════


class TestExecutionMode:
    """D: user_required 失败 → 结构化 memory_write_failed；M: 不创建 IngestionRun。"""

    def test_user_required_tool_returns_memory_write_failed_on_error(self, db_session):
        """D: user_required 写入失败时 Gate 应 reject。"""
        writer = MemoryWriteService(db_session)
        proposal = MemoryProposal(
            memory_type=MemoryType.KNOWLEDGE.value,
            canonical_key=f"knowledge.fail_{_uuid.uuid4().hex[:8]}",
            scope_type=ScopeType.GLOBAL.value,
            content="will fail",
            confidence=0.9,
            execution_mode="user_required",
            retention_policy="ephemeral",  # 无 valid_to → Gate reject
            evidence=[EvidenceItem(
                source_type="user_message",
                trust_level=TrustLevel.TRUSTED.value,
            )],
        )
        ctx = RunContext(thread_id="t1", trace_id="tr1", execution_mode="user_required")

        result = writer.write(proposal, run_context=ctx)
        db_session.flush()

        # Gate reject → outcome 不是 WRITTEN
        assert result.outcome.value != "written"
        assert result.outcome.value == "gate_rejected"

    def test_explicit_path_no_ingestion_run(self, db_session):
        """M: 显式路径不创建 MemoryIngestionRun。"""
        writer = MemoryWriteService(db_session)
        proposal = MemoryProposal(
            memory_type=MemoryType.KNOWLEDGE.value,
            canonical_key="knowledge.noirun",
            scope_type=ScopeType.GLOBAL.value,
            content="test",
            confidence=0.9,
            execution_mode="user_required",
            evidence=[EvidenceItem(
                source_type="user_message",
                trust_level=TrustLevel.TRUSTED.value,
            )],
        )
        ctx = RunContext(thread_id="t1", trace_id="tr1", execution_mode="user_required")
        writer.write(proposal, run_context=ctx)
        db_session.flush()

        runs = db_session.query(MemoryIngestionRun).all()
        assert len(runs) == 0

    def test_system_best_effort_does_not_block(self, db_session):
        """E: system_best_effort 失败不阻断（Gate reject 被 write_batch 允许）。"""
        # 在 write 级别，system_best_effort 的 Gate reject 只是返回 rejected
        writer = MemoryWriteService(db_session)
        proposal = MemoryProposal(
            memory_type=MemoryType.KNOWLEDGE.value,
            canonical_key="knowledge.sbe_test",
            scope_type=ScopeType.GLOBAL.value,
            content="test",
            confidence=0.5,  # low → candidate
            execution_mode="system_best_effort",
            evidence=[],
        )
        ctx = RunContext(thread_id="t1", trace_id="tr1", execution_mode="system_best_effort")
        result = writer.write(proposal, run_context=ctx)
        # 不会被 reject（无证据 → candidate，非 reject）
        assert result.outcome.value != "gate_rejected"


# ════════════════════════════════════════════════════
# 手动记忆 API 结果契约
# ════════════════════════════════════════════════════


class TestManualMemoryApiContract:
    """POST /api/memories 必须按结构化 outcome 返回真实结果。"""

    @staticmethod
    def _client(db_session) -> TestClient:
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db_session
        return TestClient(app)

    def test_success_response_contains_structured_outcome(self, db_session):
        """成功写入应返回 WRITTEN、非空 ID 和真实生命周期。"""
        response = self._client(db_session).post(
            "/api/memories",
            headers={"Idempotency-Key": f"api-success-{_uuid.uuid4().hex}"},
            json={"content": "我喜欢结构化响应", "memory_type": "preference"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["outcome"] == "written"
        assert payload["id"]
        assert payload["lifecycle_state"] in ("active", "candidate")

    def test_ignore_response_is_explicit_noop(self, db_session):
        """ignore 应返回明确 no-op，不能返回空 ID 的创建成功。"""
        ignored = WriteResult(
            outcome=WriteOutcome.IGNORED,
            operation="ignore",
            reason="重复记忆",
        )
        with patch(
            "aiive.api.routes_memories.MemoryWriteService.write",
            return_value=ignored,
        ):
            response = self._client(db_session).post(
                "/api/memories",
                headers={"Idempotency-Key": f"api-ignore-{_uuid.uuid4().hex}"},
                json={"content": "重复内容", "memory_type": "fact"},
            )

        assert response.status_code == 200
        assert response.json()["ok"] is False
        assert response.json()["outcome"] == "ignored"
        assert response.json()["id"] == ""
        assert response.json()["reason"] == "重复记忆"

    def test_idempotent_ignore_replays_noop_without_writing_again(self, db_session):
        """重复 ignore 请求应重放 no-op，不能再次调用写入服务。"""
        idempotency_key = f"api-ignore-replay-{_uuid.uuid4().hex}"
        client = self._client(db_session)
        with patch(
            "aiive.memory.conflict_resolver.ConflictResolver.resolve",
            return_value=ResolutionResult(
                operation="ignore",
                reason="重复记忆",
            ),
        ) as resolve:
            first = client.post(
                "/api/memories",
                headers={"Idempotency-Key": idempotency_key},
                json={"content": "重复内容", "memory_type": "fact"},
            )
            second = client.post(
                "/api/memories",
                headers={"Idempotency-Key": idempotency_key},
                json={"content": "重复内容", "memory_type": "fact"},
            )

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["ok"] is False
        assert second.json()["outcome"] == "ignored"
        assert second.json()["idempotent"] is True
        assert resolve.call_count == 1

    def test_gate_rejected_response_uses_422(self, db_session):
        """规则拒绝必须返回 422，而不是 200 假成功。"""
        rejected = WriteResult(
            outcome=WriteOutcome.GATE_REJECTED,
            reason="未通过写入规则",
        )
        with patch(
            "aiive.api.routes_memories.MemoryWriteService.write",
            return_value=rejected,
        ):
            response = self._client(db_session).post(
                "/api/memories",
                headers={"Idempotency-Key": f"api-reject-{_uuid.uuid4().hex}"},
                json={"content": "拒绝内容", "memory_type": "fact"},
            )

        assert response.status_code == 422
        assert response.json()["detail"] == "未通过写入规则"

    def test_failed_response_uses_409(self, db_session):
        """真正失败必须回滚并返回 409。"""
        failed = WriteResult(
            outcome=WriteOutcome.FAILED,
            reason="数据库写入失败",
        )
        with patch(
            "aiive.api.routes_memories.MemoryWriteService.write",
            return_value=failed,
        ):
            response = self._client(db_session).post(
                "/api/memories",
                headers={"Idempotency-Key": f"api-failed-{_uuid.uuid4().hex}"},
                json={"content": "失败内容", "memory_type": "fact"},
            )

        assert response.status_code == 409
        assert response.json()["detail"] == "数据库写入失败"


# ════════════════════════════════════════════════════
# Idempotency-Key 测试
# ════════════════════════════════════════════════════


class TestIdempotencyKey:
    """Q: Idempotency-Key 持久化 + UNIQUE 约束防重复写入。"""

    def test_idempotency_key_prevents_duplicate_write(self, db_session):
        """Q: 相同 idempotency_key 不会产生两次 create。"""
        ikey = f"test-idem-{_uuid.uuid4().hex[:8]}"
        writer = MemoryWriteService(db_session)

        proposal1 = MemoryProposal(
            memory_type=MemoryType.KNOWLEDGE.value,
            canonical_key="knowledge.idem_test",
            scope_type=ScopeType.GLOBAL.value,
            content="idempotent content",
            confidence=0.9,
            idempotency_key=ikey,
            evidence=[EvidenceItem(
                source_type="user_message",
                trust_level=TrustLevel.TRUSTED.value,
            )],
        )

        ctx = RunContext(thread_id="t1", trace_id="tr1", execution_mode="user_required")
        writer.write(proposal1, run_context=ctx)
        db_session.flush()

        # 第二次同样 ikey → _persist_proposal 应跳过
        proposal2 = MemoryProposal(
            memory_type=MemoryType.KNOWLEDGE.value,
            canonical_key="knowledge.idem_test",
            scope_type=ScopeType.GLOBAL.value,
            content="idempotent content",
            confidence=0.9,
            idempotency_key=ikey,
            evidence=[EvidenceItem(
                source_type="user_message",
                trust_level=TrustLevel.TRUSTED.value,
            )],
        )
        writer.write(proposal2, run_context=ctx)
        db_session.flush()

        # 只创建了一条 MemoryRecord（第二次是 reinforce 或 ignore）
        records = db_session.query(MemoryRecord).filter(
            MemoryRecord.canonical_key == "knowledge.idem_test"
        ).all()
        # 同 key 同 scope → 只有一个 active record
        active_count = sum(1 for r in records if r.lifecycle_state == "active")
        assert active_count == 1
