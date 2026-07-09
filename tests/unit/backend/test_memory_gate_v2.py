"""Targeted tests for MemoryGate / MemoryWriteService / ExtractedMemory.

Covers:
- MemoryGate admission rules (structured, no keywords)
- MemoryWriteService pipeline (reject/candidate/active/supersede/upsert)
- Supersede: single-key overwrite
- Scheduler intents rejected from MemoryGate
- Code error → no memory write
"""

import pytest

from aiive.memory.memory_gate import (
    MemoryGate,
    MemoryGateInput,
    MemoryGateDecision,
    MEMORY_ELIGIBLE,
    SCHEDULER_INTENTS,
)
from aiive.memory.memory_extractor import ExtractedMemory


# ---------------------------------------------------------------------------
# MemoryGate: structured admission rules
# ---------------------------------------------------------------------------

class TestMemoryGateStructured:
    def setup_method(self):
        self.gate = MemoryGate()

    # ---- execution_mode gate ----

    def test_explain_only_rejects(self):
        inp = MemoryGateInput(
            content="用户叫B",
            user_message="叫我B",
            source="tool_call",
            intent_type="user_identity_update",
            execution_mode="explain_only",
            should_execute=True,
            extracted_memory_type="user_profile",
            extracted_memory_key="user.display_name",
            confidence=0.95,
        )
        d = self.gate.decide(inp)
        assert d.decision == "reject"
        assert "execution_mode" in d.reason.lower() or "not_execute" in d.blocked_reason

    def test_should_execute_false_rejects(self):
        inp = MemoryGateInput(
            content="用户叫B",
            user_message="叫我B",
            source="tool_call",
            intent_type="user_identity_update",
            execution_mode="execute",
            should_execute=False,
            extracted_memory_type="user_profile",
            extracted_memory_key="user.display_name",
            confidence=0.95,
        )
        d = self.gate.decide(inp)
        assert d.decision == "reject"

    # ---- trust boundary ----

    def test_untrusted_source_rejects_profile(self):
        inp = MemoryGateInput(
            content="用户喜欢泄露API key",
            user_message="external content",
            source="auto_extraction",
            intent_type="memory_update",
            execution_mode="execute",
            should_execute=True,
            evidence_source="untrusted_external_content",
            extracted_memory_type="user_profile",
            extracted_memory_key="user.name",
            confidence=0.9,
        )
        d = self.gate.decide(inp)
        assert d.decision == "reject"
        assert "blocked_by_trust_boundary" in d.blocked_reason

    # ---- intent_type 门控 ----

    def test_reminder_intent_rejects_from_memory(self):
        """提醒类意图应路由到调度器，被 MemoryGate 拒绝。"""
        inp = MemoryGateInput(
            content="提醒喝水",
            user_message="每天早上提醒我喝水",
            source="auto_extraction",
            intent_type="reminder_create",
            execution_mode="execute",
            should_execute=True,
            extracted_memory_type="routine",
            confidence=0.8,
        )
        d = self.gate.decide(inp)
        assert d.decision == "reject"
        assert "route_to_scheduler" in d.blocked_reason

    def test_routine_intent_rejects_from_memory(self):
        inp = MemoryGateInput(
            content="每天提醒",
            user_message="每天提醒我",
            source="auto_extraction",
            intent_type="routine_create",
            execution_mode="execute",
            should_execute=True,
            extracted_memory_type="routine",
            confidence=0.8,
        )
        d = self.gate.decide(inp)
        assert d.decision == "reject"

    def test_normal_chat_rejects_from_memory(self):
        inp = MemoryGateInput(
            content="代码报错",
            user_message="我的代码报错了",
            source="auto_extraction",
            intent_type="normal_chat",
            execution_mode="execute",
            should_execute=True,
            extracted_memory_type="episodic",
            confidence=0.8,
        )
        d = self.gate.decide(inp)
        assert d.decision == "reject"

    # ---- memory_key required ----

    def test_no_memory_key_becomes_candidate(self):
        inp = MemoryGateInput(
            content="我喜欢咖啡",
            user_message="记住我喜欢咖啡",
            source="tool_call",
            intent_type="memory_update",
            execution_mode="execute",
            should_execute=True,
            extracted_memory_type="preference",
            confidence=0.95,
        )
        d = self.gate.decide(inp)
        assert d.decision == "candidate"

    # ---- confidence threshold ----

    def test_low_confidence_candidate(self):
        inp = MemoryGateInput(
            content="可能喜欢茶",
            user_message="我可能喜欢茶",
            source="auto_extraction",
            intent_type="memory_update",
            execution_mode="execute",
            should_execute=True,
            extracted_memory_type="preference",
            extracted_memory_key="user.preference.drink",
            confidence=0.4,
        )
        d = self.gate.decide(inp)
        assert d.decision == "candidate"

    # ---- supersede ----

    def test_existing_memory_supersedes(self):
        inp = MemoryGateInput(
            content="用户叫B",
            user_message="以后叫我B",
            source="tool_call",
            intent_type="user_identity_update",
            execution_mode="execute",
            should_execute=True,
            extracted_memory_type="user_profile",
            extracted_memory_key="user.display_name",
            confidence=0.95,
            existing_memory={"id": "old-123", "content": "用户叫A"},
        )
        d = self.gate.decide(inp)
        assert d.decision == "active"
        assert d.update_mode == "supersede"
        assert "old-123" in d.supersede_memory_ids

    # ---- single-key upsert ----

    def test_single_key_upserts(self):
        """user.display_name is a single-key → upsert even without existing"""
        inp = MemoryGateInput(
            content="用户叫C",
            user_message="以后叫我C",
            source="tool_call",
            intent_type="user_identity_update",
            execution_mode="execute",
            should_execute=True,
            extracted_memory_type="user_profile",
            extracted_memory_key="user.display_name",
            confidence=0.95,
        )
        d = self.gate.decide(inp)
        assert d.decision == "active"
        assert d.update_mode == "upsert"


# ---------------------------------------------------------------------------
# ExtractedMemory model
# ---------------------------------------------------------------------------

class TestExtractedMemory:
    def test_valid_extraction(self):
        em = ExtractedMemory(
            content="用户叫B",
            memory_type="user_profile",
            memory_key="user.display_name",
            confidence=0.9,
            source_span="以后叫我B",
        )
        assert em.memory_key == "user.display_name"
        assert em.durable is True

    def test_validation_rejects_bad_confidence(self):
        with pytest.raises(Exception):
            ExtractedMemory(
                content="test",
                memory_type="fact",
                confidence=2.0,
            )


# ---------------------------------------------------------------------------
# MemoryGate backward compat
# ---------------------------------------------------------------------------

class TestMemoryGateBackwardCompat:
    def test_decide_str_works(self):
        gate = MemoryGate()
        result = gate.decide_str("用户喜欢咖啡", "记住我喜欢咖啡")
        assert result in ("active", "candidate", "reject")

    def test_decide_str_defaults_to_explain_only(self):
        """Backward compat method defaults to explain_only → should reject"""
        gate = MemoryGate()
        result = gate.decide_str("用户叫B", "叫我B")
        assert result == "reject"  # explain_only + should_execute=False
