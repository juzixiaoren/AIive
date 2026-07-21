"""ProposalNormalizer / UnifiedMemoryExtractor：assistant 事件 provenance 区分回归测试。

覆盖：
1. normalize：传入 assistant_event_ids 时，其中的事件标记为 llm_reply，其余保持 user_message。
2. normalize：不传 assistant_event_ids 时，所有 source_event_ids 仍标记为 user_message（旧行为兼容）。
3. extract：含 source_span 时，evidence 仍逐事件指向真实 Event.id，并把 span 附到
   user_message 证据项，assistant 事件标为 llm_reply（span/event 互补，方案 B）。
4. extract：未提供真实 source_event_ids 时退回 trace_id 兜底，且不把 trace_id
   作为 evidence 的 source_event_id 落库（仅保留 span 证据）。
"""

import json

from aiive.core.llm_client import FakeLLMClient
from aiive.memory.memory_extractor import UnifiedMemoryExtractor
from aiive.memory.proposal_normalizer import ProposalNormalizer


def test_assistant_event_ids_tagged_as_llm_reply():
    """属于 assistant 回复的事件应标记为 llm_reply，用户消息保持 user_message。"""
    normalizer = ProposalNormalizer()
    result = normalizer.normalize(
        content="user prefers Python",
        memory_type_hint="knowledge",
        source_event_ids=["ev_user", "ev_assistant"],
        assistant_event_ids=["ev_assistant"],
    )
    assert result.proposal is not None
    by_id = {e.source_event_id: e.source_type for e in result.proposal.evidence}
    assert by_id["ev_user"] == "user_message"
    assert by_id["ev_assistant"] == "llm_reply"


def test_no_assistant_event_ids_keeps_user_message():
    """未区分时，所有 source_event_ids 维持旧有的 user_message 标注（兼容旧调用方）。"""
    normalizer = ProposalNormalizer()
    result = normalizer.normalize(
        content="user prefers Python",
        memory_type_hint="knowledge",
        source_event_ids=["ev_user", "ev_other"],
    )
    assert result.proposal is not None
    assert all(e.source_type == "user_message" for e in result.proposal.evidence)


def _extractor_with_span() -> UnifiedMemoryExtractor:
    """构造返回单条含 source_span 结果的伪抽取器。"""
    fixed = json.dumps([{
        "content": "用户主要用 Python 开发",
        "memory_type": "knowledge",
        "memory_key": "knowledge.lang.python",
        "confidence": 0.9,
        "importance": 0.7,
        "source_span": "我主要用 Python 开发",
        "durable": True,
    }])
    return UnifiedMemoryExtractor(FakeLLMClient(fixed_content=fixed))


def test_extract_span_and_events_complementary():
    """含 span 时：evidence 逐事件指向真实 Event.id，span 附到 user_message，assistant 标 llm_reply。"""
    extractor = _extractor_with_span()
    proposals = extractor.extract(
        user_message="我主要用 Python 开发",
        reply="好的，已记住你用 Python。",
        trace_id="trace-1",
        thread_id="thread-1",
        source_event_ids=["ev_user", "ev_assistant"],
        assistant_event_ids=["ev_assistant"],
    )
    assert len(proposals) == 1
    ev = proposals[0].evidence
    by_id = {e.source_event_id: e for e in ev}
    # 两条 evidence 均指向真实事件
    assert set(by_id.keys()) == {"ev_user", "ev_assistant"}
    assert by_id["ev_user"].source_type == "user_message"
    assert by_id["ev_assistant"].source_type == "llm_reply"
    # span 附到 user_message 证据项
    assert by_id["ev_user"].content_span == "我主要用 Python 开发"
    assert by_id["ev_assistant"].content_span in (None, "")
    # 幂等键基于真实事件计算
    assert proposals[0].source_event_ids == ["ev_user", "ev_assistant"]


def test_extract_without_real_events_falls_back_to_trace_id():
    """无真实事件时退回 trace_id，且不把 trace_id 作为 evidence 的 source_event_id 落库。"""
    extractor = _extractor_with_span()
    proposals = extractor.extract(
        user_message="我主要用 Python 开发",
        reply="好的。",
        trace_id="trace-1",
        thread_id="thread-1",
    )
    assert len(proposals) == 1
    # source_event_ids 退回 trace_id
    assert proposals[0].source_event_ids == ["trace-1"]
    # evidence 仅保留 span，不落库 trace_id 作为 source_event_id
    assert all(e.source_event_id in (None, "") for e in proposals[0].evidence)
    assert any(e.content_span == "我主要用 Python 开发" for e in proposals[0].evidence)
