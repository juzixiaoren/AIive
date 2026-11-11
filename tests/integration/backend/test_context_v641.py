"""V64.1 integration: history-only sealing, actual injection, and summary hard gates."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from aiive.config import settings
from aiive.db.models import CompactionRun, SegmentSummary, Segment
from aiive.runtime.context_assembler import ContextAssembler, ContextBudgetExceededError
from aiive.runtime.context_budget import ContextBudget
from aiive.runtime.thread_state import ThreadState
from aiive.runtime.turn_execution import TurnExecutionService
from aiive.runtime.token_models import ModelProfile, TokenCount
from aiive.runtime.tool_normalizer import ToolResultNormalizer
from aiive.memory.recall_models import MemoryRecallPack
from aiive.retrieval.retrieval_types import RetrievalHit
from aiive.runtime.compaction import validate_compaction_prompt, CompactionPromptExceededError
from tests.integration.backend._util import new_thread, new_epoch, new_segment, add_turn, add_event, claim_job
from tests.integration.backend.test_phase3_handlers import _setup_sealable_segment, _find_segment_sealing_job
from tests.unit.backend.test_context_overflow import CharacterCounter


def _service(counter, window=32000, output=4096):
    svc = object.__new__(TurnExecutionService)
    svc._token_counter = counter
    svc._profile = ModelProfile("test", "test", "test/test", window, output)
    svc._budget = ContextBudget(window, output)
    svc._normalizer = ToolResultNormalizer(counter, "test/test")
    return svc


@pytest.mark.parametrize("kind", ["short", "token", "turn32", "other_segment", "half_window"])
def test_history_only_segment_sealing(db, monkeypatch, kind):
    thread = new_thread(db); epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    counter = CharacterCounter(); svc = _service(counter)
    monkeypatch.setattr(settings, "aiive_segment_seal_token_threshold", 200 if kind != "half_window" else 64000)
    count = 32 if kind == "turn32" else 1
    for seq in range(1, count + 1):
        turn = add_turn(db, thread.id, seg.id, seq)
        text = "x" * (300 if kind == "token" else 15000 if kind == "half_window" else 2)
        add_event(db, thread.id, turn.turn_id, "user_message", {"content": text})
    if kind == "other_segment":
        other = new_segment(db, epoch.id, thread.id, segment_no=2, start=count + 1)
        other_turn = add_turn(db, thread.id, other.id, count + 1)
        add_event(db, thread.id, other_turn.turn_id, "user_message", {"content": "x" * 10000})
    svc._mark_segment_pending_seal(db, turn, datetime.now(timezone.utc))
    db.expire_all()
    assert (db.get(Segment, seg.id).pending_seal_at is not None) == (kind in ("token", "turn32", "half_window"))
    if kind == "token":
        _, stats = ThreadState(db).load_recent_messages_bounded(db, thread.id, None, counter, "test", segment_id=seg.id)
        assert 200 <= stats.estimated_tokens < svc._budget.hard_input_limit * 0.8


def test_thread_events_all_carry_turn_id_and_bounded_count(db):
    thread = new_thread(db); epoch = new_epoch(db, thread.id); seg = new_segment(db, epoch.id, thread.id)
    turn = add_turn(db, thread.id, seg.id, 1, source="system_command")
    for i, (etype, payload) in enumerate([
        ("user_message", {"content": "sys"}), ("tool_call", {"name": "search", "tool_call_id": "c"}),
        ("tool_result", {"name": "search", "tool_call_id": "c", "result": {"a": 1}}),
        ("llm_response", {"content": "done"}),
    ]): add_event(db, thread.id, turn.turn_id, etype, payload, i)
    counter = CharacterCounter()
    events, stats = ThreadState(db).load_recent_messages_bounded(db, thread.id, None, counter, "test")
    assert {e["type"] for e in events} == {"system", "tool_call", "tool_result", "assistant"}
    assert all(e["turn_id"] == turn.turn_id for e in events)
    assert stats.estimated_tokens == counter.count_messages("test", ThreadState._dicts_to_chat_messages(events)).estimated_tokens
    events, stats = ThreadState(db).load_recent_messages_bounded(db, thread.id, None, counter, "test", max_events=2)
    assert not events and stats.stopped_by == "max_events"


def test_only_final_injected_memories_are_touched(db, monkeypatch):
    thread = new_thread(db)
    asm = ContextAssembler(CharacterCounter(), ContextBudget(2200, 100))
    hits = [RetrievalHit(source_type="memory_record", source_id=name, source_version="1",
                        final_score=score, snippet=name * 350, title=name, memory_record_id=name)
            for name, score in [("low", 0.1), ("high", 0.9)]]
    pack = MemoryRecallPack(request_id="r")
    monkeypatch.setattr(asm, "_load_agent_context", lambda *a, **k: {
        "stable_contract_text": "contract", "core_blocks": [], "recall_pack": pack, "retrieval_hits": hits,
    })
    monkeypatch.setattr(asm, "_build_tools_schema_list", lambda *a: [])
    monkeypatch.setattr("aiive.runtime.context_assembler.AttentionManager.resolve_for_turn", lambda *a, **k: {})
    touched = Mock()
    monkeypatch.setattr("aiive.memory.memory_access_tracker.MemoryAccessTracker.touch", touched)
    assembled = asm.assemble(db, "CURRENT", thread)
    assert [item["memory_id"] for item in assembled.snapshot.injected_memory_ids] == ["high"]
    touched.assert_called_once_with(["high"])
    trace = assembled.snapshot.selection_trace
    assert trace["policy_version"] == "v64.2.lexicographic.1"
    ds = {d["source_id"]: d for d in trace["candidates"]}
    assert ds["high"]["selected"] and not ds["low"]["selected"]
    assert ds["low"]["exclusion_reason"] == "context_budget_exceeded"
    assert "lowlow" not in json.dumps(trace)
    assert "lowlow" not in json.dumps(assembled.snapshot.full_contents)
    assert "lowlow" not in json.dumps(assembled.messages)
    assert "highhigh" in json.dumps(assembled.messages)
    assert all(report.hard_limit is None for report in assembled.partition_reports if report.name != "total")
    assert any(report.trimmed and "low" in report.trim_reason for report in assembled.partition_reports)
    with pytest.raises(ContextBudgetExceededError):
        asm.assemble(db, "x" * 10000, thread)
    touched.assert_called_once()


@pytest.mark.parametrize("retry", [False, True])
def test_summary_overflow_seals_with_deterministic_fallback(db, monkeypatch, retry, caplog):
    from aiive.worker import outbox_handlers as h
    from aiive.runtime.epoch_manager import EpochManager
    thread, epoch, seg, ws = _setup_sealable_segment(db)
    EpochManager().begin_segment_sealing(db, thread.id, seg.id); db.commit()
    llm = Mock()
    llm.chat.return_value = NS(content="bad json " * 30000)
    monkeypatch.setattr(h, "_get_llm_client", lambda: llm)
    monkeypatch.setattr(settings, "aiive_llm_context_window", 32000)
    monkeypatch.setattr(settings, "aiive_llm_max_output_tokens", 4096)
    if not retry:
        original = h._build_summary_prompt
        monkeypatch.setattr(h, "_build_summary_prompt", lambda *a: original(*a) + [{"role": "user", "content": "huge " * 50000}])
    job = _find_segment_sealing_job(db, seg.id)
    result = h.handle_segment_sealing(claim_job(db, job))
    assert result.outcome.value == "completed"
    assert llm.chat.call_count == (1 if retry else 0)
    db.expire_all()
    sealed = db.get(Segment, seg.id)
    assert sealed.status == "sealed"
    summary = db.get(SegmentSummary, sealed.summary_id)
    assert summary.model_id == "deterministic-extractive-fallback"
    assert summary.source_hash == sealed.source_hash
    assert summary.important_tool_results[0]["tool_call_id"] == "tc1"
    assert db.query(CompactionRun).filter_by(segment_id=seg.id).one().status == "succeeded"
    assert "compaction_prompt_overflow" in caplog.text


def test_forget_summary_gate_returns_existing_safe_fallback(db, monkeypatch):
    from aiive.worker import outbox_handlers as h, handlers_forget as f
    thread = new_thread(db); epoch = new_epoch(db, thread.id); seg = new_segment(db, epoch.id, thread.id)
    turn = add_turn(db, thread.id, seg.id, 1)
    add_event(db, thread.id, turn.turn_id, "user_message", {"content": "retained"})
    llm = Mock()
    monkeypatch.setattr("aiive.core.llm_client.LLMClient", lambda **k: llm)
    monkeypatch.setattr(settings, "aiive_llm_context_window", 32000)
    monkeypatch.setattr(settings, "aiive_llm_max_output_tokens", 4096)
    monkeypatch.setattr(h, "_build_summary_prompt", lambda *a: [{"role": "user", "content": "large " * 50000}])
    assert f._try_generate_summary(db, seg.id, "forget-op") is None
    llm.chat.assert_not_called()


def test_production_compaction_counter_counts_safe_margin(monkeypatch):
    monkeypatch.setattr(settings, "aiive_llm_context_window", 32000)
    monkeypatch.setattr(settings, "aiive_llm_max_output_tokens", 4096)
    validate_compaction_prompt([{"role": "user", "content": "short"}])
    with pytest.raises(CompactionPromptExceededError):
        validate_compaction_prompt([{"role": "user", "content": "large " * 50000}])


def test_auto_uses_global_candidate_budget_and_complete_visible_sources(db, monkeypatch):
    from aiive.retrieval.unified_retriever import UnifiedRetriever
    from aiive.retrieval.retrieval_types import RetrievalRequest
    from aiive.memory.recall_models import ScopeContext
    from tests.unit.backend.test_v2_recall import _mk
    from aiive.db.forget_models import ForgetTombstone
    from tests.integration.backend.test_forget_v624_scope import _op
    long_text = "budget " * 2000
    record = _mk(db, "project.budget", long_text)
    engine = UnifiedRetriever(db)
    result = engine.retrieve(RetrievalRequest(query="project.budget", scope_context=ScopeContext(), token_budget=28000))
    assert len(result.hits) == 1
    assert result.hits[0].snippet == long_text
    assert result.hits[0].token_count > 1200
    # A small caller-supplied global bound is honoured; no fixed auto override.
    small = engine.retrieve(RetrievalRequest(query="project.budget", scope_context=ScopeContext(), token_budget=100))
    assert not small.hits
    op_id = _op(db, mode="forget", selector="memory", payload={"memory_ids": [record.id]})
    db.add(ForgetTombstone(forget_operation_id=op_id, target_type="memory_record", target_id=record.id,
                          block_visibility=True, block_reingestion=True))
    db.flush()
    hidden = engine.retrieve(RetrievalRequest(query="project.budget", scope_context=ScopeContext(), token_budget=28000))
    assert not hidden.hits
    asm = ContextAssembler(CharacterCounter(), ContextBudget(5000, 100))
    monkeypatch.setattr(asm, "_load_agent_context", lambda *a, **k: {
        "stable_contract_text": "contract", "core_blocks": [], "recall_pack": None, "retrieval_hits": hidden.hits,
    })
    monkeypatch.setattr(asm, "_build_tools_schema_list", lambda *a: [])
    assembled = asm.assemble(db, "project.budget", new_thread(db))
    assert record.id not in json.dumps(assembled.snapshot.selection_trace)
    assert long_text not in json.dumps(assembled.messages)


def test_core_blocks_are_complete_and_not_limited_to_600_tokens(db):
    from tests.unit.backend.test_v2_recall import _mk
    from aiive.memory.core_memory_projection import build_blocks
    text = "identity " * 2000
    _mk(db, "core.human_identity", text, memory_type="user_profile")
    blocks = build_blocks(db)
    assert len(blocks) == 1
    assert text in blocks[0].content
    assert blocks[0].token_count > 600


def test_auto_history_is_not_limited_to_800_tokens(db):
    from tests.integration.backend.test_phase5_retrieval import _active_gen, _attach_summary_source
    from aiive.retrieval.indexing_service import refresh_source
    from aiive.retrieval.unified_retriever import UnifiedRetriever
    from aiive.retrieval.retrieval_types import RetrievalRequest
    from aiive.memory.recall_models import ScopeContext
    _active_gen(db)
    thread = new_thread(db, id="t1"); epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    text = "historybudget " * 2000
    summary = SegmentSummary(segment_id=seg.id, goal=text, source_hash="h", summary_version=1)
    db.add(summary); db.flush()
    seg.status = "sealed"; seg.summary_id = summary.id
    _attach_summary_source(db, summary)
    refresh_source(db, "segment_summary", summary.id, "segment.sealed")
    result = UnifiedRetriever(db).retrieve(RetrievalRequest(query="historybudget", scope_context=ScopeContext(), thread_id=thread.id, token_budget=28000))
    found = next(hit for hit in result.hits if hit.source_id == summary.id)
    assert text in json.loads(found.snippet)["goal"]
    assert found.token_count > 800
