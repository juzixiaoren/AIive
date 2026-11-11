"""V64.2 policy acceptance: deterministic ranks, protection and global decisions."""
from types import SimpleNamespace as NS

from aiive.runtime.context_overflow import (
    ContextCandidates,
    ContextOverflowPacker,
    HistoryTurn,
)
from aiive.runtime.context_policy import (
    ContextCandidateDecision,
    eviction_key,
    rank_source,
)
from aiive.runtime.token_counter import LiteLLMTokenCounter

from tests.unit.backend.test_context_overflow import CharacterCounter, hit


def decisions(c):
    c.prepare()
    return {d["candidate_id"]: d for d in c.selection_trace["candidates"]}


def graph(sid, score, confidence=0.5, hop=1, **kw):
    return NS(relation_id=sid, source_memory_id=sid, score=score, confidence=confidence, hop=hop, **kw)


def test_rank_percentiles_and_singleton():
    ds = [ContextCandidateDecision(str(i), "s", str(i), 1) for i in range(3)]
    rank_source(ds)
    assert [d.rank_percentile for d in ds] == [1, 0.5, 0]
    rank_source(ds[:1])
    assert ds[0].rank_percentile == 1


def test_final_score_and_graph_native_order():
    c = ContextCandidates(retrieval=[hit("a", 100), hit("b", 10)],
                          graph=[graph("z", .9, .2), graph("a", .9, .8, 2), graph("b", .9, .8, 1), graph("low", .1)])
    ds = decisions(c)
    assert ds["retrieval:memory_record:a"]["source_rank"] == 1
    assert [ds[f"graph:{i}"]["source_rank"] for i in ("b", "a", "z", "low")] == [1, 2, 3, 4]
    # Graph's top .9 outranks retrieval's bottom 10 by source-relative rank.
    c.remove_next()
    assert len(c.graph) == 3  # both bottom ranks equal; graph wins stable source tie
    c.remove_next()
    assert [h.source_id for h in c.retrieval] == ["a"]


def test_repeatable_order_despite_input_permutation():
    orders = []
    for reverse in (False, True, False):
        hits = [hit("c", .5), hit("b", .5), hit("a", .9)]
        c = ContextCandidates(retrieval=list(reversed(hits)) if reverse else hits,
                              graph=[graph("r", .001)], history=[HistoryTurn("old", []), HistoryTurn("new", [])])
        order = []
        while c.remove_next():
            order.append([d["candidate_id"] for d in c.selection_trace["candidates"] if not d["selected"]])
        orders.append(order)
    assert orders[0] == orders[1] == orders[2]


def test_summary_importance_and_created_at():
    c = ContextCandidates(summaries=[NS(id="important", created_at="2020", open_loops=["todo"]),
                                     NS(id="new", created_at="2026"), NS(id="old", created_at="2021")])
    assert c.remove_next() and [s.id for s in c.summaries] == ["important", "new"]
    assert c.remove_next() and c.summaries[0].id == "important"
    assert decisions(c)["segment_summary:important"]["retention_tier"] == 2


def test_protected_state_and_optional_loop_priority():
    ws = {k: [{"fact": "confirmed"}] for k in ("pending_approvals", "active_constraints", "verified_tool_states",
                                               "running_tool_state", "uncommitted_side_effects")}
    ws.update(current_objective="goal", open_loops=[{"priority": p} for p in ("P0", "P1", "P2", "P3")], version=4)
    c = ContextCandidates(working_state=ws, history=[HistoryTurn("protected", [], True)])
    assert c.remove_next() and [v["priority"] for v in ws["open_loops"]] == ["P0", "P1", "P2"]
    assert c.remove_next() and [v["priority"] for v in ws["open_loops"]] == ["P0", "P1"]
    assert not c.remove_next()
    assert all(d["selected"] for d in decisions(c).values() if d["retention_tier"] == 3)
    assert "working_state:version:0" not in decisions(c)


def test_conflict_group_uses_strongest_member_and_scope():
    conflict = {"cardinality": "single", "is_conflicted": True, "subject_entity_id": "s", "predicate": "works_at", "scope_type": "global", "scope_id": None}
    c = ContextCandidates(graph=[graph("best", .9, **conflict), graph("worst", .1, **conflict), graph("middle", .5)])
    assert c.remove_next() and {r.relation_id for r in c.graph} == {"best", "worst"}
    assert c.remove_next() and not c.graph
    ds = decisions(c)
    assert ds["graph:best"]["exclusion_reason"] == ds["graph:worst"]["exclusion_reason"] == "context_budget_exceeded:atomic_graph_conflict"


def test_equal_value_larger_candidate_first():
    c = ContextCandidates(history=[HistoryTurn("h", [])], retrieval=[hit("m", 10000)])
    c.prepare(lambda source, item: 500 if source == "retrieval" else 10)
    assert c.remove_next() and not c.retrieval and c.history
    d = ContextCandidateDecision("x", "x", "x", 1)
    assert eviction_key([d])[2] == 0  # unknown token size


def test_budget_sufficient_leaves_everything_selected():
    c = ContextCandidates(history=[HistoryTurn("h", [])], retrieval=[hit("m", .1)])
    ContextOverflowPacker().pack(c, lambda: ([{"role": "user", "content": "ok"}], []), CharacterCounter(), "test", 10000, 20)
    assert not c.removed and all(d["selected"] for d in decisions(c).values())


def test_production_counter_hard_gate_and_trace_matches_prompt():
    c = ContextCandidates(history=[HistoryTurn("old", [{"content": "旧内容 " * 2000}]), HistoryTurn("new", [{"content": "new"}])])
    counter = LiteLLMTokenCounter()
    def render():
        return [{"role": "system", "content": "contract"}, *[{"role": "user", "content": t.events[0]["content"]} for t in c.history], {"role": "user", "content": "CURRENT"}], []
    msgs, tools, count, safe = ContextOverflowPacker().pack(c, render, counter, "gpt-4o", 1000, 100)
    assert safe and count == counter.count_messages("gpt-4o", msgs, tools)
    assert count.safe_tokens + 100 <= 1000
    assert [t.turn_id for t in c.history] == ["new"]
    assert not decisions(c)["history:old"]["selected"]
    assert decisions(c)["history:new"]["selected"]
    assert "旧内容" not in str(msgs)


def test_conflict_scopes_remain_separate():
    common = {"cardinality": "single", "is_conflicted": True, "subject_entity_id": "s", "predicate": "works_at", "scope_type": "project"}
    c = ContextCandidates(graph=[graph("a", .9, scope_id="one", **common), graph("b", .8, scope_id="one", **common),
                                graph("c", .2, scope_id="two", **common), graph("d", .1, scope_id="two", **common)])
    assert c.remove_next()
    assert {r.relation_id for r in c.graph} == {"a", "b"}
    assert c.selection_trace["eviction_order"] == ["graph:c", "graph:d"]


def test_core_role_priority_preserves_identity_last():
    c = ContextCandidates(core=[NS(block_name=n) for n in ("core.agent_persona", "core.human_identity", "core.interaction_defaults")])
    while c.remove_next():
        pass
    assert c.selection_trace["eviction_order"] == ["core:core.agent_persona", "core:core.interaction_defaults", "core:core.human_identity"]


def test_agent_graph_merge_preserves_final_selection_trace():
    from aiive.runtime.agent_graph import AgentGraph, LLMUsageStats
    from aiive.runtime.context_assembler import ContextSnapshotData

    original = ContextSnapshotData(
        full_contents={"user_message": "question"},
        selection_trace={"policy_version": "v64.2.lexicographic.1", "candidates": [
            {"candidate_id": "history:removed", "selected": False, "exclusion_reason": "context_budget_exceeded"},
        ]},
    )
    agent = object.__new__(AgentGraph)
    result = agent._assemble_agent_result(
        reply="answer", graph_state_tool_records=[], records_raw=[], pending_approvals=[],
        trace=NS(trace_id="trace"), message="question", context_snapshot=original,
        llm_usage=LLMUsageStats(), signal=None,
    )
    assert result.context_snapshot.selection_trace == original.selection_trace
    assert result.context_snapshot.full_contents["user_message"] == "question"
    assert "answer" in result.context_snapshot.full_contents.values()
