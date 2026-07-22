"""Phase 1: ContextAssembler 回归测试。

覆盖审查报告指出的关键问题:
- 历史重建时 assistant tool_calls 与 tool 结果的 tool_call_id 配对（审查项 10）
- 分区级 token 统计报告（审查项 3）
- pending_seal 语义（软阈值触发，而非"发生过裁剪"）
- ContextBudget.from_env 配置覆盖
"""
import pytest

from aiive.runtime.context_assembler import ContextAssembler
from aiive.db.models import Epoch, Segment, SegmentSummary, Thread
from aiive.runtime.context_budget import ContextBudget
from aiive.runtime.token_models import TokenCount


class _FakeTokenCounter:
    """确定性的假 TokenCounter，便于断言分区报告。"""

    def count_messages(self, model, messages, tools=None):
        # 按字符粗略估算，保证 safe_tokens 与 estimated_tokens 一致可比
        text = _dump(messages) + _dump(tools or [])
        est = max(1, len(text) // 2)
        return TokenCount(
            estimated_tokens=est,
            safety_margin_tokens=0,
            source="fake",
            confidence="high",
            model=model,
        )


def _dump(obj) -> str:
    import json as _json
    try:
        return _json.dumps(obj, ensure_ascii=False, default=str)
    except TypeError:
        return str(obj)


def _events_with_tool_call_id(tool_call_id: str = "call_abc"):
    """生成一对共享同一 tool_call_id 的 tool_call / tool_result 事件。"""
    return [
        {"type": "user", "content": "调用工具", "event_id": "e0"},
        {
            "type": "tool_call", "tool_name": "search", "tool_params": {"q": "x"},
            "tool_call_id": tool_call_id, "event_id": "e1",
        },
        {
            "type": "tool_result", "tool_name": "search", "tool_result": "结果",
            "tool_call_id": tool_call_id, "event_id": "e2",
        },
        {"type": "assistant", "content": "完成", "event_id": "e3"},
    ]


def test_tool_call_id_pairing_matches():
    """审查项 10: assistant tool_calls 与 tool 结果的 tool_call_id 必须一致。"""
    events = _events_with_tool_call_id("call_abc")
    msgs = ContextAssembler._dicts_to_chat_messages(events)

    assistant = next(m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls"))
    tool_msg = next(m for m in msgs if m.get("role") == "tool")

    assert assistant["tool_calls"][0]["id"] == "call_abc"
    assert tool_msg["tool_call_id"] == "call_abc"
    assert assistant["tool_calls"][0]["id"] == tool_msg["tool_call_id"]


def test_multiple_tool_call_ids_each_paired():
    """多个工具调用各自保留独立且一致的 tool_call_id。"""
    events = [
        {"type": "tool_call", "tool_name": "a", "tool_params": {}, "tool_call_id": "c1", "event_id": "e1"},
        {"type": "tool_call", "tool_name": "b", "tool_params": {}, "tool_call_id": "c2", "event_id": "e2"},
        {"type": "tool_result", "tool_name": "a", "tool_result": "ra", "tool_call_id": "c1", "event_id": "e3"},
        {"type": "tool_result", "tool_name": "b", "tool_result": "rb", "tool_call_id": "c2", "event_id": "e4"},
    ]
    msgs = ContextAssembler._dicts_to_chat_messages(events)
    assistant = next(m for m in msgs if m.get("tool_calls"))
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    aid_ids = [tc["id"] for tc in assistant["tool_calls"]]
    tid_ids = [m["tool_call_id"] for m in tool_msgs]
    assert set(aid_ids) == {"c1", "c2"}
    assert set(tid_ids) == {"c1", "c2"}


def test_collect_hot_history_ids_includes_sealing_bridge_summary(db_session):
    """sealing bridge 已加载的 summary 必须从 UnifiedRetriever 排除。"""
    thread = Thread(id="thread-sealing-hot")
    epoch = Epoch(id="epoch-sealing-hot", thread_id=thread.id, epoch_no=1, status="active")
    segment = Segment(
        id="segment-sealing-hot",
        thread_id=thread.id,
        epoch_id=epoch.id,
        segment_no=1,
        start_turn_sequence=1,
        status="sealing",
        summary_id="summary-sealing-hot",
    )
    summary = SegmentSummary(
        id="summary-sealing-hot",
        segment_id=segment.id,
        summary_version=1,
        source_turn_ids=[],
    )
    db_session.add_all([thread, epoch, segment, summary])
    db_session.flush()
    assembler = ContextAssembler(token_counter=_FakeTokenCounter(), budget=ContextBudget.DEFAULT)

    hot_ids = assembler._collect_hot_history_ids(db_session, thread)  # pyright: ignore[reportPrivateUsage]

    assert summary.id in hot_ids


def test_message_normalization_accepts_new_and_legacy_tool_formats():
    """共享规范化应让新旧工具格式生成相同的稳定消息契约。"""
    legacy = ContextAssembler._dicts_to_chat_messages([
        {
            "type": "tool_call", "tool_name": "search", "tool_params": {"q": "x"},
            "tool_call_id": "call-1", "event_id": "event-1",
        },
        {
            "type": "tool_result", "tool_name": "search", "tool_result": "ok",
            "tool_call_id": "call-1", "event_id": "event-2",
        },
    ])
    modern = ContextAssembler._dicts_to_chat_messages([
        {
            "type": "tool_call",
            "id": "call-1",
            "function": {"name": "search", "arguments": '{"q":"x"}'},
        },
        {
            "type": "tool_result", "id": "call-1", "name": "search", "result": "ok",
        },
    ])

    assert legacy == modern


def test_langchain_conversion_tolerates_malformed_tool_arguments():
    """畸形工具参数应保留原文，不得中断 LangChain 历史重建。"""
    from aiive.runtime.agent_graph import AgentGraph

    messages = AgentGraph._dicts_to_langchain_messages([  # pyright: ignore[reportPrivateUsage]
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call-bad",
                "type": "function",
                "function": {"name": "search", "arguments": "{bad-json"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-bad",
            "name": "search",
            "content": "error",
        },
    ])

    assert messages[0].tool_calls[0]["args"] == {"raw_arguments": "{bad-json"}
    assert messages[1].tool_call_id == "call-bad"


def test_build_reports_includes_per_partition_and_total():
    """审查项 3: _build_reports 应产出每个分区及 total 汇总。"""
    asm = ContextAssembler(token_counter=_FakeTokenCounter(), budget=ContextBudget.DEFAULT)
    total = TokenCount(estimated_tokens=100, safety_margin_tokens=0, source="fake", model="m")
    reports = asm._build_reports(
        trim_plan=__import__("aiive.runtime.context_assembler", fromlist=["TrimPlan"]).TrimPlan.from_budget(ContextBudget.DEFAULT, 0),
        total=total,
        stable_contract_text="contract",
        core_memory_text="core",
        working_state_text="ws",
        epoch_checkpoint_text="checkpoint",
        segment_summary_text="summary",
        sealing_bridge_text="bridge",
        recall_text="recall",
        history_msgs=[{"role": "user", "content": "hi"}],
        tools_schema=[{"type": "function", "function": {"name": "x"}}],
    )
    names = {r.name for r in reports}
    # 7 个分区 + total
    expected = {
        "stable_contract", "core_memory", "working_state", "epoch_checkpoint",
        "segment_summaries", "sealing_bridge", "retrieved_memory",
        "recent_messages", "tool_definitions", "tool_results", "total",
    }
    assert expected.issubset(names)
    total_report = next(r for r in reports if r.name == "total")
    assert total_report.safe_tokens == total.safe_tokens


def test_context_budget_from_env_default_without_env(monkeypatch):
    """P1-7: 未设置环境变量时 from_env 回退到默认预算。"""
    monkeypatch.delenv("AIIVE_CONTEXT_BUDGET_JSON", raising=False)
    budget = ContextBudget.from_env()
    assert budget.model_context_window == ContextBudget.DEFAULT.model_context_window
    assert budget.tool_results.hard_limit_tokens == ContextBudget.DEFAULT.tool_results.hard_limit_tokens


def test_context_budget_from_env_override(monkeypatch):
    """P1-7: 通过环境变量覆盖某分区上限。"""
    monkeypatch.setenv(
        "AIIVE_CONTEXT_BUDGET_JSON",
        '{"tool_results": {"soft_limit_tokens": 3000, "hard_limit_tokens": 5000}}',
    )
    budget = ContextBudget.from_env()
    assert budget.tool_results.soft_limit_tokens == 3000
    assert budget.tool_results.hard_limit_tokens == 5000
    # 其他分区不受影响
    assert budget.stable_contract.soft_limit_tokens == ContextBudget.DEFAULT.stable_contract.soft_limit_tokens


def test_context_budget_from_env_rejects_invalid_limit(monkeypatch):
    """环境覆盖包含非整数 token 上限时必须在加载阶段明确失败。"""
    monkeypatch.setenv(
        "AIIVE_CONTEXT_BUDGET_JSON",
        '{"tool_results": {"hard_limit_tokens": "invalid"}}',
    )

    with pytest.raises(ValueError, match="包含无效的 token 上限"):
        ContextBudget.from_env()


def test_context_budget_from_env_validates_partition_total(monkeypatch):
    """环境覆盖使分区总额超过窗口时必须立即失败。"""
    monkeypatch.setenv(
        "AIIVE_CONTEXT_BUDGET_JSON",
        '{"tool_results": {"hard_limit_tokens": 999999}}',
    )

    with pytest.raises(ValueError, match="超过"):
        ContextBudget.from_env()
