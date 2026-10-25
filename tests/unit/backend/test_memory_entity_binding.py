"""实体绑定的纯逻辑测试：解析、服务端校验、回填、单次调用。

全部离线：脚本化伪 LLM，不访问网络、不需要 API Key。
"""
from __future__ import annotations

import json
import types

from aiive.core.llm_client import LLMClientError
from aiive.memory.conflict_adjudication import (
    BIND_CREATE_NEW,
    BIND_REUSE,
    BIND_UNRESOLVED,
    EntityBinding,
    SemanticConflictAdjudicator,
    apply_entity_bindings,
    validate_entity_bindings,
)
from aiive.memory.conflict_candidates import EntityCandidate
from aiive.memory.graph_types import GraphFact
from aiive.memory.memory_types import EvidenceItem, MemoryProposal, TrustLevel

ENTITY_A = "11111111-1111-1111-1111-111111111111"
ENTITY_B = "22222222-2222-2222-2222-222222222222"


class ScriptedLLM:
    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.prompts: list[str] = []

    def chat(self, messages, model=None, temperature=None, timeout=None,
             trace_id=None, json_mode=False):
        self.calls += 1
        self.prompts.append(str(messages[-1].get("content", "")))
        index = min(self.calls - 1, len(self._responses) - 1)
        return types.SimpleNamespace(
            content=self._responses[index], finish_reason="stop",
            model="scripted", usage={}, latency_ms=1.0, trace_id=trace_id,
        )


class BoomLLM:
    def chat(self, *args, **kwargs):
        raise LLMClientError("llm down")


def _proposal(subject: str = "AIive 项目", object_: str = "Python") -> MemoryProposal:
    proposal = MemoryProposal(
        memory_type="user_profile",
        canonical_key="project.tech_stack",
        scope_type="global",
        content="AIive 项目的技术栈是 Python",
        graph_facts=[GraphFact(
            subject=subject, predicate="uses_language", object=object_,
            confidence=0.9, source_span="AIive 项目的技术栈是 Python",
        )],
        evidence=[EvidenceItem(
            source_type="user_message", trust_level=TrustLevel.TRUSTED.value,
            relation="supports", content_span="AIive 项目的技术栈是 Python",
        )],
        confidence=0.9,
        trust_level=TrustLevel.TRUSTED.value,
    )
    proposal.compute_content_hash()
    return proposal


def _candidate(entity_id: str = ENTITY_A, name: str = "AIive") -> EntityCandidate:
    return EntityCandidate(
        entity_id=entity_id,
        display_name=name,
        normalized_name=name.casefold(),
        entity_type="project",
        aliases=[],
        scope_type="global",
        scope_id=None,
        matched_by=["contains"],
        matched_names=["AIive 项目"],
    )


# ---------------------------------------------------------------------------
# 服务端校验
# ---------------------------------------------------------------------------

class TestValidateBindings:
    def test_reuse_with_candidate_id_is_accepted(self):
        result = validate_entity_bindings(
            [EntityBinding(name="AIive 项目", decision=BIND_REUSE, entity_id=ENTITY_A)],
            pending_names=["AIive 项目"],
            candidate_ids={ENTITY_A},
            relation_ids=set(),
        )

        assert len(result) == 1
        assert result[0].accepted is True
        assert result[0].entity_id == ENTITY_A

    def test_reuse_with_unknown_id_is_rejected(self):
        result = validate_entity_bindings(
            [EntityBinding(name="AIive 项目", decision=BIND_REUSE, entity_id=ENTITY_B)],
            pending_names=["AIive 项目"],
            candidate_ids={ENTITY_A},
            relation_ids=set(),
        )

        assert result[0].accepted is False
        assert result[0].decision == BIND_UNRESOLVED
        assert result[0].rejected_reason == "entity_not_in_candidates"

    def test_name_outside_pending_set_is_dropped(self):
        result = validate_entity_bindings(
            [EntityBinding(name="别的实体", decision=BIND_REUSE, entity_id=ENTITY_A)],
            pending_names=["AIive 项目"],
            candidate_ids={ENTITY_A},
            relation_ids=set(),
        )

        assert result == []

    def test_conflicting_duplicate_bindings_become_unresolved(self):
        result = validate_entity_bindings(
            [
                EntityBinding(name="AIive 项目", decision=BIND_REUSE, entity_id=ENTITY_A),
                EntityBinding(name="aiive 项目", decision=BIND_REUSE, entity_id=ENTITY_B),
            ],
            pending_names=["AIive 项目"],
            candidate_ids={ENTITY_A, ENTITY_B},
            relation_ids=set(),
        )

        assert len(result) == 1
        assert result[0].accepted is False
        assert result[0].decision == BIND_UNRESOLVED
        assert result[0].entity_id == ""
        assert result[0].rejected_reason == "conflicting_bindings"

    def test_conflicting_duplicates_are_order_independent(self):
        first = EntityBinding(name="AIive 项目", decision=BIND_REUSE, entity_id=ENTITY_A)
        second = EntityBinding(name="AIive 项目", decision=BIND_REUSE, entity_id=ENTITY_B)

        forward = validate_entity_bindings(
            [first, second], pending_names=["AIive 项目"],
            candidate_ids={ENTITY_A, ENTITY_B}, relation_ids=set(),
        )
        backward = validate_entity_bindings(
            [second, first], pending_names=["AIive 项目"],
            candidate_ids={ENTITY_A, ENTITY_B}, relation_ids=set(),
        )

        assert [(item.decision, item.entity_id) for item in forward] == [
            (item.decision, item.entity_id) for item in backward
        ]
        assert forward[0].rejected_reason == "conflicting_bindings"

    def test_identical_duplicate_bindings_are_deduped(self):
        result = validate_entity_bindings(
            [
                EntityBinding(name="AIive 项目", decision=BIND_REUSE, entity_id=ENTITY_A),
                EntityBinding(name="AIive 项目", decision=BIND_REUSE, entity_id=ENTITY_A),
            ],
            pending_names=["AIive 项目"],
            candidate_ids={ENTITY_A},
            relation_ids=set(),
        )

        assert len(result) == 1
        assert result[0].accepted is True
        assert result[0].entity_id == ENTITY_A

    def test_evidence_id_not_provided_is_dropped(self):
        result = validate_entity_bindings(
            [EntityBinding(
                name="AIive 项目", decision=BIND_REUSE, entity_id=ENTITY_A,
                evidence_relation_id="rel-not-provided",
            )],
            pending_names=["AIive 项目"],
            candidate_ids={ENTITY_A},
            relation_ids={"rel-1"},
        )

        assert result[0].evidence_relation_id == ""
        assert "evidence_not_provided" in result[0].rejected_reason
        assert result[0].accepted is True

    def test_create_new_and_unresolved(self):
        result = validate_entity_bindings(
            [
                EntityBinding(name="AIive 项目", decision=BIND_CREATE_NEW),
                EntityBinding(name="AIive 项目 2", decision="whatever"),
            ],
            pending_names=["AIive 项目", "AIive 项目 2"],
            candidate_ids=set(),
            relation_ids=set(),
        )

        assert result[0].accepted is True
        assert result[0].decision == BIND_CREATE_NEW
        assert result[1].accepted is False
        assert result[1].decision == BIND_UNRESOLVED


class TestApplyBindings:
    def test_applies_both_roles(self):
        proposal = _proposal(subject="AIive 项目", object_="Python 3")
        bindings = [
            EntityBinding(name="AIive 项目", decision=BIND_REUSE, entity_id=ENTITY_A, accepted=True),
            EntityBinding(name="Python 3", decision=BIND_REUSE, entity_id=ENTITY_B, accepted=True),
        ]

        applied = apply_entity_bindings(proposal, bindings)

        assert applied == 2
        assert proposal.graph_facts[0].subject_entity_id == ENTITY_A
        assert proposal.graph_facts[0].object_entity_id == ENTITY_B

    def test_skips_unaccepted_and_create_new(self):
        proposal = _proposal()
        bindings = [
            EntityBinding(name="AIive 项目", decision=BIND_REUSE, entity_id=ENTITY_A, accepted=False),
            EntityBinding(name="Python", decision=BIND_CREATE_NEW, accepted=True),
        ]

        applied = apply_entity_bindings(proposal, bindings)

        assert applied == 0
        assert proposal.graph_facts[0].subject_entity_id is None
        assert proposal.graph_facts[0].object_entity_id is None


# ---------------------------------------------------------------------------
# 同一次调用：冲突裁决 + 实体绑定
# ---------------------------------------------------------------------------

class TestAdjudicatorBinding:
    def test_single_call_returns_verdict_and_bindings(self):
        fake = ScriptedLLM(json.dumps({
            "verdict": "compatible",
            "reason": "同一项目的技术栈描述",
            "question": "",
            "confidence": 0.8,
            "entity_bindings": [{
                "name": "AIive 项目",
                "decision": "reuse",
                "entity_id": ENTITY_A,
                "confidence": 0.9,
                "reason": "同一项目",
            }],
        }, ensure_ascii=False))

        adjudication = SemanticConflictAdjudicator(fake).adjudicate(
            proposal=_proposal(),
            candidates=[],
            graph_evidence=[],
            entity_candidates=[_candidate()],
            pending_entity_names=["AIive 项目"],
            trace_id="t-1",
        )

        assert fake.calls == 1
        assert adjudication.verdict == "compatible"
        assert len(adjudication.entity_bindings) == 1
        assert adjudication.entity_bindings[0].entity_id == ENTITY_A

    def test_prompt_contains_candidate_ids_and_names(self):
        fake = ScriptedLLM('{"verdict": "compatible", "entity_bindings": []}')

        SemanticConflictAdjudicator(fake).adjudicate(
            proposal=_proposal(),
            candidates=[],
            graph_evidence=[],
            entity_candidates=[_candidate()],
            pending_entity_names=["AIive 项目"],
        )

        prompt = fake.prompts[0]
        assert ENTITY_A in prompt
        assert "AIive 项目" in prompt
        assert "entity_bindings" in prompt
        assert "不得编造" in prompt

    def test_degraded_call_produces_no_bindings(self):
        adjudication = SemanticConflictAdjudicator(BoomLLM()).adjudicate(
            proposal=_proposal(),
            candidates=[],
            graph_evidence=[],
            entity_candidates=[_candidate()],
            pending_entity_names=["AIive 项目"],
        )

        assert adjudication.degraded is True
        assert adjudication.entity_bindings == []

    def test_unparseable_binding_entries_are_ignored(self):
        fake = ScriptedLLM(json.dumps({
            "verdict": "compatible",
            "entity_bindings": [
                "not-a-dict",
                {"decision": "reuse"},
                {"name": "AIive 项目", "decision": "nonsense", "entity_id": ENTITY_A},
            ],
        }, ensure_ascii=False))

        adjudication = SemanticConflictAdjudicator(fake).adjudicate(
            proposal=_proposal(),
            candidates=[],
            graph_evidence=[],
            entity_candidates=[_candidate()],
            pending_entity_names=["AIive 项目"],
        )

        assert len(adjudication.entity_bindings) == 1
        assert adjudication.entity_bindings[0].decision == BIND_UNRESOLVED


def _two_fact_proposal() -> MemoryProposal:
    """两条事实共用同一个主体名称，但上下文指向不同实体。"""
    proposal = MemoryProposal(
        memory_type="user_profile",
        canonical_key="project.mercury",
        scope_type="global",
        content="水星是行星；信使计划代号也是 Mercury",
        graph_facts=[
            GraphFact(
                subject="Mercury", predicate="is_planet", object="planet",
                confidence=0.9, source_span="水星是行星",
            ),
            GraphFact(
                subject="Mercury", predicate="is_codename", object="mission",
                confidence=0.9, source_span="信使计划代号也是 Mercury",
            ),
        ],
        evidence=[EvidenceItem(
            source_type="user_message", trust_level=TrustLevel.TRUSTED.value,
            relation="supports", content_span="水星是行星",
        )],
        confidence=0.9,
        trust_level=TrustLevel.TRUSTED.value,
    )
    proposal.compute_content_hash()
    return proposal


class TestBindingTargets:
    """同一名称多次出现时，绑定必须能落到具体的事实位置。"""

    FACTS = [("Mercury", "planet"), ("Mercury", "mission")]

    def _validate(self, bindings, *, pending=None):
        return validate_entity_bindings(
            bindings,
            pending_names=pending if pending is not None else ["Mercury"],
            candidate_ids={ENTITY_A, ENTITY_B},
            relation_ids=set(),
            fact_names=self.FACTS,
        )

    def test_fact_level_binding_targets_single_occurrence(self):
        proposal = _two_fact_proposal()
        bindings = self._validate([EntityBinding(
            name="Mercury", decision=BIND_REUSE, entity_id=ENTITY_B,
            fact_index=1, role="subject",
        )])

        applied = apply_entity_bindings(proposal, bindings)

        assert applied == 1
        assert proposal.graph_facts[0].subject_entity_id is None
        assert proposal.graph_facts[1].subject_entity_id == ENTITY_B

    def test_name_level_binding_covers_all_occurrences(self):
        proposal = _two_fact_proposal()
        bindings = self._validate([EntityBinding(
            name="Mercury", decision=BIND_REUSE, entity_id=ENTITY_A,
        )])

        applied = apply_entity_bindings(proposal, bindings)

        assert applied == 2
        assert proposal.graph_facts[0].subject_entity_id == ENTITY_A
        assert proposal.graph_facts[1].subject_entity_id == ENTITY_A

    def test_fact_level_binding_with_wrong_position_is_dropped(self):
        # facts[0] 的客体是 planet，不是 Mercury → 位置与名称不一致，整条丢弃
        result = self._validate([EntityBinding(
            name="Mercury", decision=BIND_REUSE, entity_id=ENTITY_B,
            fact_index=0, role="object",
        )])

        assert result == []

    def test_fact_level_binding_with_out_of_range_index_is_dropped(self):
        result = self._validate([EntityBinding(
            name="Mercury", decision=BIND_REUSE, entity_id=ENTITY_B,
            fact_index=9, role="subject",
        )])

        assert result == []

    def test_fact_level_binding_for_already_resolved_name_is_dropped(self):
        result = self._validate(
            [EntityBinding(
                name="Mercury", decision=BIND_REUSE, entity_id=ENTITY_B,
                fact_index=1, role="subject",
            )],
            pending=[],
        )

        assert result == []

    def test_name_level_binding_is_superseded_by_fact_level(self):
        proposal = _two_fact_proposal()
        bindings = self._validate([
            EntityBinding(name="Mercury", decision=BIND_REUSE, entity_id=ENTITY_A),
            EntityBinding(
                name="Mercury", decision=BIND_REUSE, entity_id=ENTITY_B,
                fact_index=1, role="subject",
            ),
        ])

        by_target = {(item.fact_index, item.role): item for item in bindings}
        assert by_target[(None, "")].rejected_reason == "superseded_by_fact_binding"
        assert by_target[(1, "subject")].accepted is True

        apply_entity_bindings(proposal, bindings)
        assert proposal.graph_facts[0].subject_entity_id is None
        assert proposal.graph_facts[1].subject_entity_id == ENTITY_B


class TestBindingParsing:
    def test_parse_reads_fact_index_and_role(self):
        fake = ScriptedLLM(json.dumps({
            "verdict": "compatible",
            "entity_bindings": [{
                "name": "Mercury", "decision": "reuse", "entity_id": ENTITY_B,
                "fact_index": 1, "role": "subject", "confidence": 0.9,
            }],
        }, ensure_ascii=False))

        adjudication = SemanticConflictAdjudicator(fake).adjudicate(
            proposal=_two_fact_proposal(),
            candidates=[],
            graph_evidence=[],
            entity_candidates=[_candidate(ENTITY_B, "Mercury")],
            pending_entity_names=["Mercury"],
        )

        binding = adjudication.entity_bindings[0]
        assert binding.fact_index == 1
        assert binding.role == "subject"

    def test_parse_ignores_bad_fact_index_and_role(self):
        fake = ScriptedLLM(json.dumps({
            "verdict": "compatible",
            "entity_bindings": [
                {"name": "Mercury", "decision": "reuse", "entity_id": ENTITY_A,
                 "fact_index": "abc", "role": "middle"},
                {"name": "Mercury", "decision": "reuse", "entity_id": ENTITY_A,
                 "fact_index": True, "role": "subject"},
            ],
        }, ensure_ascii=False))

        adjudication = SemanticConflictAdjudicator(fake).adjudicate(
            proposal=_two_fact_proposal(),
            candidates=[],
            graph_evidence=[],
            entity_candidates=[_candidate(ENTITY_A, "Mercury")],
            pending_entity_names=["Mercury"],
        )

        for binding in adjudication.entity_bindings:
            assert binding.fact_index is None
            assert binding.role == ""
