"""V64.1 source-aware overflow: whole units, real recount, and protected facts."""
import json
from types import SimpleNamespace as NS

from aiive.runtime.context_overflow import ContextCandidates, ContextOverflowPacker, HistoryTurn
from aiive.runtime.context_assembler import ContextAssembler
from aiive.runtime.token_models import TokenCount
from aiive.retrieval.retrieval_types import RetrievalHit


class CharacterCounter:
    def __init__(self):
        self.calls = []

    def count_messages(self, model, messages, tools=None):
        self.calls.append((messages, tools))
        size = len(json.dumps([messages, tools or []], ensure_ascii=False, default=str))
        return TokenCount(size, 7)


def hit(name, score, source="memory_record"):
    return RetrievalHit(source_id=name, source_type=source, source_version="1", final_score=score)


def test_overflow_order_and_protected_state():
    protected = {"current_objective": "goal", "pending_approvals": [{"action": "approve"}],
                 "active_constraints": [{"description": "never delete"}],
                 "uncommitted_side_effects": [{"tool_call_id": "pending"}],
                 "running_tool_state": [{"tool_call_id": "running"}],
                 "open_loops": [{"priority": "P0", "description": "critical"}, {"priority": "P3"}],
                 "artifact_refs": [{"ref": "artifact://old"}]}
    c = ContextCandidates(
        history=[HistoryTurn("uncertain", [], True), HistoryTurn("old", []), HistoryTurn("new", [])],
        retrieval=[hit("high", 0.9), hit("low", 0.1, "segment_summary")],
        graph=[NS(score=0.9, hop=0, confidence=0.8, source_memory_id="near"),
               NS(score=0.9, hop=2, confidence=0.7, source_memory_id="far")],
        summaries=[NS(id="old-summary"), NS(id="new-summary")],
        working_state=protected,
        core=[NS(block_name="core.human_identity"), NS(block_name="core.agent_persona")],
    )
    assert c.remove_next() and c.history[-1].turn_id == "new"
    assert c.remove_next() and len(c.history) == 1
    assert c.remove_next() and c.retrieval[0].source_id == "high"
    assert c.remove_next() and not c.retrieval
    assert c.remove_next() and c.graph[0].source_memory_id == "near"
    assert c.remove_next() and not c.graph
    assert c.remove_next() and c.summaries[0].id == "new-summary"
    assert c.remove_next() and not c.summaries
    assert c.remove_next() and not protected["artifact_refs"]
    assert c.remove_next() and protected["open_loops"] == [{"priority": "P0", "description": "critical"}]
    assert c.remove_next() and c.core[0].block_name == "core.human_identity"
    assert c.remove_next() and not c.core
    assert not c.remove_next()
    assert protected["current_objective"] == "goal"
    assert all(protected[key] for key in ("pending_approvals", "active_constraints", "uncommitted_side_effects", "running_tool_state"))


def test_packer_recounts_complete_turns_and_preserves_tool_pairs():
    def events(tid, content):
        return [{"turn_id": tid, "type": "user", "content": content},
                {"turn_id": tid, "type": "tool_call", "tool_name": "search", "tool_params": {"q": "{"}, "tool_call_id": tid},
                {"turn_id": tid, "type": "tool_result", "tool_name": "search", "tool_result": {"json": [1, 2]}, "tool_call_id": tid},
                {"turn_id": tid, "type": "assistant", "content": "done"}]
    c = ContextCandidates(history=[HistoryTurn("old", events("old", "x" * 500)), HistoryTurn("new", events("new", "y"))])
    def render():
        history = ContextAssembler._dicts_to_chat_messages([e for turn in c.history for e in turn.events])
        return [{"role": "system", "content": "contract"}, *history, {"role": "user", "content": "CURRENT"}], []
    counter = CharacterCounter()
    msgs, _, count, safe = ContextOverflowPacker().pack(c, render, counter, "test", 600, 20)
    assert safe and count.safe_tokens + 20 <= 600
    assert len(counter.calls) == 2
    assert [turn.turn_id for turn in c.history] == ["new"]
    assert [m["tool_call_id"] for m in msgs if m["role"] == "tool"] == ["new"]
    call = next(m["tool_calls"][0] for m in msgs if m.get("tool_calls"))
    assert json.loads(call["function"]["arguments"]) == {"q": "{"}
    assert msgs[-1]["content"] == "CURRENT"


def test_required_content_overflow_is_unsafe():
    c = ContextCandidates()
    msgs = [{"role": "user", "content": "x" * 1000}]
    _, _, _, safe = ContextOverflowPacker().pack(c, lambda: (msgs, []), CharacterCounter(), "test", 100, 20)
    assert not safe
