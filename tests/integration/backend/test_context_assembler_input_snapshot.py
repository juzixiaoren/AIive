"""回归测试：ContextAssembler.assemble() 必须填充强类型上下文快照。

输入与输出快照统一由 ContextSnapshotData 承载，避免字典 meta 与恒空列表双轨。
"""
from __future__ import annotations

import pytest

from aiive.runtime.context_assembler import (
    ContextAssembler,
    ContextSnapshotData,
    ContextSnapshotItem,
)
from aiive.memory.recall_config import RecallConfig
from aiive.memory.recall_models import ScopeContext
from aiive.retrieval.retrieval_types import RetrievalResult, RetrievalHit
from aiive.memory.recall_models import CoreMemoryBlock
from aiive.db.models import SegmentSummary
from aiive.runtime.context_budget import ContextBudget
from aiive.runtime.token_models import ModelProfile, TokenCount
from tests.integration.backend._util import new_thread, new_epoch, new_segment


class _FakeTokenCounter:
    """返回远低于预算的固定 token 数，确保一次通过 hard gate。"""

    def count_messages(self, model, messages, tools=None) -> TokenCount:
        return TokenCount(
            estimated_tokens=100,
            safety_margin_tokens=0,
            safe_tokens=100,
        )


class _StubWorkingStateService:
    def context_fields(self, *args, **kwargs):
        return {}

    def render_context_fields(self, fields) -> str:
        return "OPEN LOOPS: 完成上下文快照修复"


def _build_budget() -> ContextBudget:

    return ContextBudget(model_context_window=200_000, reserved_output_tokens=2_000)


def _make_assembler() -> ContextAssembler:
    return ContextAssembler(
        token_counter=_FakeTokenCounter(),
        budget=_build_budget(),
        profile=ModelProfile(
            provider="deepseek", model_id="deepseek-chat",
            full_name="deepseek/deepseek-chat",
            context_window=200_000, max_output_tokens=2_000,
        ),
    )


@pytest.mark.parametrize("scenario", ["no_hits", "degraded", "budget_dropped"])
def test_memory_state_matches_the_final_prompt(db, monkeypatch, scenario):
    from aiive.retrieval.unified_retriever import UnifiedRetriever

    class Counter(_FakeTokenCounter):
        def count_messages(self, model, messages, tools=None):
            total = 900 if any("very large recall hit" in message.get("content", "") for message in messages) else 100
            return TokenCount(estimated_tokens=total, safe_tokens=total, safety_margin_tokens=0)

    degraded = scenario == "degraded"
    hits = [RetrievalHit(source_type="memory_record", source_id="small", source_version="1", snippet="small hit", final_score=0.9),
            RetrievalHit(source_type="memory_record", source_id="large", source_version="1", snippet="very large recall hit", final_score=0.1)
            ] if scenario == "budget_dropped" else []
    monkeypatch.setattr(UnifiedRetriever, "retrieve", lambda _, req: RetrievalResult(
        request_id=req.request_id, hits=hits, degraded=degraded,
        notes=["memory_route_degraded"] if degraded else [],
    ))
    if degraded:
        def broken_core(*args):
            raise RuntimeError("internal error detail")

        monkeypatch.setattr("aiive.runtime.context_assembler.load_core_memory", broken_core)
    assembler = ContextAssembler(token_counter=Counter(), budget=ContextBudget(
        model_context_window=750, reserved_output_tokens=100,
    ))
    thread = new_thread(db)
    assembled = assembler.assemble(db, "test", thread)
    state, = [m["content"] for m in assembled.messages if m["content"].startswith("## Memory State")]
    assert assembled.snapshot.full_contents["memory_state"] == state
    if degraded:
        assert "Core Memory: degraded" in state
        assert "Automatic Recall: degraded, hits=0, injected=0, budget_dropped=0" in state
        assert "core_load_failed" in state and "memory_route_degraded" in state
        assert "internal error detail" not in state
    elif hits:
        assert "hits=2, injected=1, budget_dropped=1" in state
        assert not any("very large recall hit" in m["content"] for m in assembled.messages)
        assert any("small hit" in m["content"] for m in assembled.messages)
    else:
        assert "Core Memory: loaded" in state
        assert "Automatic Recall: completed, hits=0, injected=0, budget_dropped=0" in state


def test_unified_recall_diagnostic_failure_does_not_retry_or_pollute_session(db, monkeypatch) -> None:
    """诊断短事务失败不得触发二次检索，也不得污染调用方 Session。"""
    import aiive.retrieval.unified_retriever as retriever_module
    import aiive.runtime.context_assembler as assembler_module

    thread = new_thread(db)
    db.commit()
    calls = {"count": 0}

    def _retrieve(_self, request):
        calls["count"] += 1
        return RetrievalResult(request_id=request.request_id, hits=[])

    class _BrokenDiagnosticsSession:
        def add(self, _value):
            raise RuntimeError("diagnostics unavailable")

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(retriever_module.UnifiedRetriever, "retrieve", _retrieve)
    monkeypatch.setattr(assembler_module, "SessionLocal", lambda: _BrokenDiagnosticsSession())
    assembler = _make_assembler()

    pack, history, hits = assembler._unified_recall(
        db,
        "Python",
        ScopeContext(thread_id=thread.id),
        thread.id,
        RecallConfig(),
        trace_id="trace-diagnostics-failure",
    )

    assert calls["count"] == 1
    assert pack.items == []
    assert history == ""
    assert db.is_active is True
    assert db.get(type(thread), thread.id) is not None


def test_assemble_populates_input_snapshot(db, monkeypatch) -> None:
    """assemble() 必须产出非空的输入分区快照，且 meta 携带 full_contents。"""
    import aiive.runtime.context_assembler as ca_mod

    thread = new_thread(db)
    db.commit()

    assembler = _make_assembler()

    # 屏蔽重型私有方法，注入可预期的分区文本
    assembler._load_agent_context = lambda db, message, thread, trace_id=None: {
        "system_content": "系统前缀契约内容",
        "recall_messages": [{"role": "system", "content": "召回记忆：用户偏好中文"}],
        "recall_pack": None,
        "history_summary_text": "历史摘要：讨论过性能优化",
        "stable_contract_text": "系统前缀契约内容",
        "core_memory_text": "核心记忆：用户名 Daniel",
        "core_blocks": [CoreMemoryBlock(
            block_name="core.human_identity", content="用户名 Daniel", token_count=4, source_memory_ids=["core-source"],
        )],
        "retrieval_hits": [
            RetrievalHit(source_type="memory_record", source_id="m1", source_version="1", snippet="用户偏好中文"),
            RetrievalHit(source_type="segment_summary", source_id="s1", source_version="1", snippet="讨论过性能优化"),
        ],
        "recall_text": "召回记忆：用户偏好中文",
    }
    assembler._load_history_bounded = lambda *a, **k: [
        {"role": "user", "content": "上一条用户消息"},
        {"role": "assistant", "content": "上一条模型回复"},
    ]
    assembler._dicts_to_prompt_messages = lambda events, zone='UTC': list(events)
    assembler._load_epoch_checkpoint = lambda *a, **k: "纪元检查点摘要"
    assembler._recent_visible_segment_summaries = lambda *a, **k: [SegmentSummary(id="summary", goal="分段摘要内容")]
    assembler._load_sealing_bridge = lambda *a, **k: ""
    monkeypatch.setattr("aiive.runtime.context_assembler.function_definitions", lambda *a, **k: [
        {"type": "function", "function": {"name": "search_memory", "parameters": {}}},
    ])
    assembler._touch_injected_memory = lambda *a, **k: None
    monkeypatch.setattr(ca_mod, "WorkingStateService", _StubWorkingStateService)

    message = "当前用户消息\n  第二行\n\n第四行\n第五行"
    assembled = assembler.assemble(db=db, message=message, thread=thread, upper_bound_sequence=0)

    items = assembled.snapshot.items
    kinds = {item.kind for item in items}
    ids = {item.item_id for item in items}
    full = assembled.snapshot.full_contents

    # 快照非空，且覆盖关键输入分区
    assert items, "输入分区快照不应为空"
    assert {"stable_prefix", "core_memory", "attention", "working_state", "epoch_checkpoint",
            "segment_summary", "history_summary", "history_user",
            "history_assistant", "recall_memory", "tool_schemas",
            "user_message"} <= kinds

    # 每个 item 均为强类型快照项
    for item in items:
        assert isinstance(item, ContextSnapshotItem)
        assert item.token_estimate >= 1
        assert item.preview_length == len(full[item.item_id])

    # 召回记忆标记为 untrusted（本轮证据非系统指令）
    recall_item = next(item for item in items if item.kind == "recall_memory")
    assert recall_item.trust_level == "untrusted"

    # full_contents 覆盖所有 item，且当前用户消息完整保留
    assert ids <= set(full.keys())
    assert full["user_message"] == assembled.messages[-1]["content"]
    user_item = next(item for item in items if item.item_id == "user_message")
    assert user_item.content_preview.startswith("[time=unknown zone=UTC source=user]")
    assert assembled.messages[-1]["content"].endswith("\n" + message)

    # 注意力上下文已注入模型消息与快照
    assert any("注意力上下文" in message.get("content", "") for message in assembled.messages)
    assert "当前用户消息" in full["attention"]

    # stable_prefix_hash 已计算
    assert assembled.snapshot.stable_prefix_hash
    assert assembled.snapshot.source_memory_ids == ["core-source"]


def test_long_tool_schema_call_and_result_previews_preserve_original_content():
    from aiive.runtime.agent_graph import AgentGraph

    long_text = "数" * 900
    tools = [{"type": "function", "function": {"name": "example", "description": long_text}}]
    input_snapshot = _make_assembler()._build_input_snapshot(tools_schema=tools)
    output_items, full_output = AgentGraph._build_post_context_items(
        "首行\r\n\r\n  三\r\n四\r\n五",
        [{"name": "example", "params": {"q": long_text}, "result": {"text": long_text}, "status": "ok"}],
    )

    assert output_items[0].content_preview == "首行\r\n\r\n  三\r\n四"
    assert output_items[0].preview_length == len(full_output["agent_output"])
    assert full_output["tool_call:0"].startswith("名称: example\n参数: ")
    assert full_output["tool_result:0"].startswith("名称: example\n状态: ok\n结果: ")
    full = {**input_snapshot.full_contents, **full_output}
    for item in [*input_snapshot.items, *output_items[1:]]:
        content = full[item.item_id]
        assert long_text in content
        assert item.content_preview == content[:800]
        assert item.preview_length == len(content)


def test_attention_failure_is_fail_open(db, monkeypatch) -> None:
    """注意力解析失败不得阻断上下文组装或污染当前会话。"""
    import aiive.runtime.context_assembler as ca_mod

    thread = new_thread(db)
    db.commit()
    assembler = _make_assembler()
    assembler._load_agent_context = lambda db, message, thread, trace_id=None: {
        "system_content": "系统前缀", "recall_messages": [], "recall_pack": None,
        "history_summary_text": "", "stable_contract_text": "系统前缀",
        "core_memory_text": "", "recall_text": "",
    }
    assembler._load_history_bounded = lambda *a, **k: []
    assembler._dicts_to_prompt_messages = lambda events, zone='UTC': list(events)
    assembler._load_epoch_checkpoint = lambda *a, **k: ""
    assembler._load_segment_summaries = lambda *a, **k: ""
    assembler._load_sealing_bridge = lambda *a, **k: ""
    monkeypatch.setattr("aiive.runtime.context_assembler.function_definitions", lambda *a, **k: [])
    assembler._touch_injected_memory = lambda *a, **k: None
    monkeypatch.setattr(ca_mod, "WorkingStateService", _StubWorkingStateService)
    monkeypatch.setattr(
        ca_mod.AttentionManager,
        "resolve_for_turn",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("attention unavailable")),
    )

    assembled = assembler.assemble(db=db, message="继续工作", thread=thread)

    assert assembled.is_safe is True
    assert all(item.kind != "attention" for item in assembled.snapshot.items)
    assert db.is_active is True


def test_input_snapshot_skips_empty_partitions(db, monkeypatch) -> None:
    """空分区不应产生 context_item（避免展示空白条目）。"""
    import aiive.runtime.context_assembler as ca_mod

    thread = new_thread(db)
    db.commit()

    assembler = _make_assembler()
    assembler._load_agent_context = lambda db, message, thread, trace_id=None: {
        "system_content": "系统前缀",
        "recall_messages": [], "recall_pack": None,
        "history_summary_text": "", "stable_contract_text": "系统前缀",
        "core_memory_text": "", "recall_text": "",
    }
    assembler._load_history_bounded = lambda *a, **k: []
    assembler._dicts_to_prompt_messages = lambda events, zone='UTC': list(events)
    assembler._load_epoch_checkpoint = lambda *a, **k: ""
    assembler._load_segment_summaries = lambda *a, **k: ""
    assembler._load_sealing_bridge = lambda *a, **k: ""
    monkeypatch.setattr("aiive.runtime.context_assembler.function_definitions", lambda *a, **k: [])
    assembler._touch_injected_memory = lambda *a, **k: None
    monkeypatch.setattr(ca_mod, "WorkingStateService", _StubWorkingStateService)

    assembled = assembler.assemble(
        db=db, message="消息", thread=thread, upper_bound_sequence=0,
    )

    kinds = {item.kind for item in assembled.snapshot.items}
    # 仅 stable_prefix + working_state(stub 返回非空) + user_message
    assert "stable_prefix" in kinds
    assert "user_message" in kinds
    assert "core_memory" not in kinds
    assert "recall_memory" not in kinds
    assert "tool_schemas" not in kinds


def test_rotate_snapshot_persists_context_items(db) -> None:
    """端到端：_rotate_snapshot 必须把强类型快照完整落库。"""
    from aiive.db.models import ContextSnapshot, TurnRecord
    from aiive.runtime.agent_graph import AgentGraphResult
    from aiive.runtime.turn_execution import TurnExecutionService

    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id, status="open")
    turn = TurnRecord(
        thread_id=thread.id, turn_id="t1", turn_sequence=1,
        status="running", epoch_id=epoch.id, segment_id=seg.id,
    )
    db.add(turn)
    db.commit()

    ag_result = AgentGraphResult(
        reply="回复", trace_id="trace-1", user_message="消息",
        context_snapshot=ContextSnapshotData(
            stable_prefix_hash="abc123",
            selection_trace={"policy_version": "v64.2.lexicographic.1", "candidates": [{"candidate_id": "history:old", "selected": False}]},
            items=[
                ContextSnapshotItem("stable_prefix", "stable_prefix", "system", "trusted", "系统前缀"),
                ContextSnapshotItem("user_message", "user_message", "user", "trusted", "消息"),
                ContextSnapshotItem("agent_output", "agent_output", "agent", "trusted", "回复"),
                ContextSnapshotItem("tool_call:0", "tool_call", "tools", "trusted", "查询工具"),
                ContextSnapshotItem("tool_result:0", "tool_result", "tools", "trusted", "查询结果"),
            ],
            full_contents={
                "stable_prefix": "系统前缀契约全文",
                "user_message": "消息",
                "agent_output": "完整回复",
                "tool_call:0": "名称: search_memory\n参数: {\"query\": \"测试\"}",
                "tool_result:0": "名称: search_memory\n状态: completed\n结果: []",
            },
        ),
    )

    service = TurnExecutionService.__new__(TurnExecutionService)
    service._rotate_snapshot(db, turn, ag_result, assembled_ctx=None)
    db.commit()

    snap = (
        db.query(ContextSnapshot)
        .filter(ContextSnapshot.thread_id == thread.id)
        .order_by(ContextSnapshot.created_at.desc())
        .first()
    )
    assert snap is not None
    assert snap.context_items, "落库的 context_items 不应为空"
    assert {it["kind"] for it in snap.context_items} == {
        "stable_prefix", "user_message", "agent_output", "tool_call", "tool_result",
    }
    assert snap.stable_prefix_hash == "abc123"
    assert snap.meta["selection_trace"] == ag_result.context_snapshot.selection_trace
    assert snap.meta.get("full_contents", {}).get("stable_prefix") == "系统前缀契约全文"
    assert snap.meta.get("full_contents", {}).get("agent_output") == "完整回复"
    assert "参数" in snap.meta.get("full_contents", {}).get("tool_call:0", "")
    assert "结果" in snap.meta.get("full_contents", {}).get("tool_result:0", "")


def test_memory_recall_candidate_requires_run_and_cascades(db) -> None:
    """召回候选必须从属于召回运行，删除运行时同步清理候选。"""
    import pytest
    from sqlalchemy.exc import IntegrityError

    from aiive.db.models import MemoryRecallCandidate, MemoryRecallRun

    orphan = MemoryRecallCandidate(
        run_id="missing-run", memory_id="memory-1", route="memory_record",
    )
    db.add(orphan)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    run = MemoryRecallRun(id="recall-run", request_query="测试查询")
    candidate = MemoryRecallCandidate(
        run_id=run.id, memory_id="memory-1", route="memory_record",
    )
    db.add(run)
    db.commit()
    db.add(candidate)
    db.commit()
    candidate_id = candidate.id

    db.delete(run)
    db.commit()
    assert db.get(MemoryRecallCandidate, candidate_id) is None
