"""冲突裁决纯逻辑：语义结论 → 自动处理/转确认，以及回复解析的降级行为。

全部离线：不访问数据库、不访问网络。
"""
from __future__ import annotations

import types

import pytest
from aiive.core.llm_client import LLMClientError
from aiive.memory.conflict_adjudication import (
    ACTION_DEFER,
    ACTION_PROCEED,
    DECISION_ACCEPT_NEW,
    DECISION_MERGE,
    DECISION_UNRELATED,
    VERDICT_COMPATIBLE,
    VERDICT_COMPLEMENTARY,
    VERDICT_CONDITIONAL,
    VERDICT_CONTRADICTION,
    VERDICT_TEMPORAL,
    VERDICT_UNCERTAIN,
    ConflictAdjudication,
    ConflictQuestionGenerator,
    ConflictReplyParser,
    SemanticConflictAdjudicator,
    adjudication_to_plan,
)
from aiive.memory.conflict_candidates import ConflictCandidate


class _Scripted:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        return types.SimpleNamespace(
            content=self._responses[min(self.calls - 1, len(self._responses) - 1)],
            finish_reason="stop",
        )


class _Boom:
    def chat(self, messages, **kwargs):
        raise LLMClientError(message="boom", status_code=500, trace_id="t")


def _candidate(key: str = "user.lives_in", trust: str = "trusted") -> ConflictCandidate:
    return ConflictCandidate(
        memory_id="m-old", content="用户住在北京", canonical_key=key,
        memory_type="user_profile", scope_type="global", scope_id=None,
        record_version=1, confidence=0.9, trust_level=trust,
    )


def _graph(predicate: str, differs: bool = True) -> list[dict]:
    return [{
        "predicate": predicate,
        "cardinality": "single" if predicate in ("lives_in", "works_at") else "multi",
        "object_differs": differs,
    }]


def _plan(verdict: str, *, key: str = "user.profile.city", graph=None, trust="trusted",
          candidates=None, degraded=False):
    adjudication = ConflictAdjudication(
        verdict=verdict, reason="r", question="q", confidence=0.6, degraded=degraded,
    )
    return adjudication_to_plan(
        adjudication,
        canonical_key=key,
        graph_evidence=graph or [],
        proposal_trust=trust,
        candidates=candidates if candidates is not None else [_candidate()],
    )


class TestAdjudicationPlan:
    @pytest.mark.parametrize("verdict", [VERDICT_COMPATIBLE, VERDICT_COMPLEMENTARY, VERDICT_TEMPORAL])
    def test_non_conflicting_verdicts_proceed(self, verdict):
        assert _plan(verdict).action == ACTION_PROCEED

    def test_uncertain_defers_to_user(self):
        result = _plan(VERDICT_UNCERTAIN)
        assert result.action == ACTION_DEFER
        assert "uncertain_needs_confirmation" in result.notes

    def test_contradiction_on_single_valued_graph_predicate_auto_resolves(self):
        result = _plan(VERDICT_CONTRADICTION, graph=_graph("lives_in"))
        assert result.action == ACTION_PROCEED

    def test_contradiction_on_multi_valued_predicate_defers(self):
        result = _plan(VERDICT_CONTRADICTION, graph=_graph("likes"))
        assert result.action == ACTION_DEFER

    def test_contradiction_with_untrusted_source_defers(self):
        result = _plan(
            VERDICT_CONTRADICTION, graph=_graph("lives_in"),
            candidates=[_candidate(trust="semi_trusted")],
        )
        assert result.action == ACTION_DEFER

    def test_contradiction_on_single_key_auto_resolves(self):
        result = _plan(VERDICT_CONTRADICTION, key="user.display_name")
        assert result.action == ACTION_PROCEED

    def test_conditional_on_multi_valued_can_coexist(self):
        result = _plan(VERDICT_CONDITIONAL, graph=_graph("likes"))
        assert result.action == ACTION_PROCEED
        assert "conditional_multi_valued_coexist" in result.notes

    def test_conditional_on_single_valued_defers(self):
        result = _plan(VERDICT_CONDITIONAL, graph=_graph("lives_in"))
        assert result.action == ACTION_DEFER

    def test_degraded_adjudication_never_blocks_existing_flow(self):
        result = _plan(VERDICT_UNCERTAIN, degraded=True)
        assert result.action == ACTION_PROCEED
        assert "adjudication_degraded_proceed" in result.notes


class TestAdjudicator:
    def test_parses_structured_verdict(self):
        llm = _Scripted('{"verdict": "temporal_change", "reason": "搬家了", "question": "", "confidence": 0.8}')
        result = SemanticConflictAdjudicator(llm).adjudicate(
            proposal=types.SimpleNamespace(canonical_key="user.profile.city", memory_type="user_profile",
                                           scope_type="global", scope_id=None, content="上海"),
            candidates=[_candidate()],
            graph_evidence=[],
        )
        assert result.verdict == VERDICT_TEMPORAL
        assert result.degraded is False

    def test_llm_failure_marks_degraded(self):
        result = SemanticConflictAdjudicator(_Boom()).adjudicate(
            proposal=types.SimpleNamespace(canonical_key="k", memory_type="t",
                                           scope_type="global", scope_id=None, content="c"),
            candidates=[], graph_evidence=[],
        )
        assert result.degraded is True
        assert result.verdict == VERDICT_UNCERTAIN

    def test_unknown_verdict_is_degraded(self):
        llm = _Scripted('{"verdict": "maybe"}')
        result = SemanticConflictAdjudicator(llm).adjudicate(
            proposal=types.SimpleNamespace(canonical_key="k", memory_type="t",
                                           scope_type="global", scope_id=None, content="c"),
            candidates=[], graph_evidence=[],
        )
        assert result.degraded is True


class TestReplyParser:
    def test_parses_accept_new(self):
        llm = _Scripted('{"decision": "accept_new", "reason": "以新的为准", "confidence": 0.9}')
        decision = ConflictReplyParser(llm).parse(
            question="q", old_content="北京", new_content="上海", user_reply="以新的为准",
        )
        assert decision.decision == DECISION_ACCEPT_NEW

    def test_parses_code_fenced_json(self):
        llm = _Scripted('\u0060\u0060\u0060json\n{"decision": "merge", "merged_content": "用户现在住在上海", "confidence": 0.7}\n\u0060\u0060\u0060')
        decision = ConflictReplyParser(llm).parse(
            question="q", old_content="北京", new_content="上海", user_reply="合起来记",
        )
        assert decision.decision == DECISION_MERGE
        assert decision.merged_content == "用户现在住在上海"

    def test_unparseable_reply_is_treated_as_unrelated(self):
        llm = _Scripted("我觉得都行")
        decision = ConflictReplyParser(llm).parse(
            question="q", old_content="北京", new_content="上海", user_reply="随便",
        )
        assert decision.decision == DECISION_UNRELATED
        assert decision.degraded is True

    def test_llm_failure_is_treated_as_unrelated(self):
        decision = ConflictReplyParser(_Boom()).parse(
            question="q", old_content="北京", new_content="上海", user_reply="以新的为准",
        )
        assert decision.decision == DECISION_UNRELATED


class TestQuestionGenerator:
    def test_llm_failure_falls_back_to_deterministic_question(self):
        question = ConflictQuestionGenerator(_Boom()).generate(
            old_content="用户住在北京", new_content="用户住在上海", reason="无法判断",
        )
        assert "北京" in question and "上海" in question
        assert "以哪一条为准" in question
