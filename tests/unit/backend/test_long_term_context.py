"""长期单会话的压缩、检查点与历史检索回归测试。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from aiive.db.models import (
    EpochCheckpoint,
    EpochCompactionInput,
    RetrievalIndexEntry,
    SegmentSummary,
)
from aiive.retrieval.retrieval_store import build_entry_fields
from aiive.retrieval.retrieval_types import RetrievalMode, RetrievalRequest
from aiive.retrieval.unified_retriever import UnifiedRetriever
from aiive.worker.outbox_handlers import (
    _build_epoch_checkpoint_payload,
    _build_extract_fallback_summary,
    _reconstruct_source_turns,
)


def test_reconstruct_source_turns_follows_frozen_manifest_order():
    older = SimpleNamespace(
        id="event-old",
        turn_id="z-random-uuid",
        turn_event_index=0,
        event_type="user_message",
        payload={"content": "先发生"},
    )
    newer = SimpleNamespace(
        id="event-new",
        turn_id="a-random-uuid",
        turn_event_index=0,
        event_type="user_message",
        payload={"content": "后发生"},
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [newer, older]

    turns = _reconstruct_source_turns(
        db,
        [{"event_id": "event-old"}, {"event_id": "event-new"}],
    )

    assert [turn["user_message"] for turn in turns] == ["先发生", "后发生"]


def test_extract_fallback_summary_is_bounded_and_non_inferential():
    summary = _build_extract_fallback_summary([
        {"user_message": "目标 A", "assistant_reply": "结果 A"},
        {"user_message": "目标 B", "assistant_reply": "结果 B"},
    ])

    assert summary == {
        "goal": "目标 A\n目标 B",
        "outcome": "结果 A\n结果 B",
        "decisions": [],
        "entities": [],
        "tool_result_summaries": [],
        "failure_explanations": [],
    }


def test_epoch_checkpoint_preserves_recent_semantic_facts():
    epoch_input = EpochCompactionInput(
        epoch_id="epoch-1",
        boundary_turn_sequence=20,
        working_state_version=3,
        current_objective=None,
        open_loops=[{"description": "继续测试"}],
        active_constraints=[{"description": "不要自动发布"}],
        artifact_refs=[{"ref": "artifact://report"}],
        verified_tool_states=[],
        source_segment_ids=["segment-1", "segment-2"],
        source_hashes=[],
        snapshot_hash="hash",
        checkpoint_version=1,
    )
    summaries = [
        SegmentSummary(
            segment_id="segment-1",
            goal="搭建长期上下文",
            outcome="完成基础压缩",
            decisions=[{"what": "保留原始回溯入口", "by": "user"}],
            entities=[{"name": "AIive", "type": "project"}],
            source_hash="hash-1",
        ),
        SegmentSummary(
            segment_id="segment-2",
            goal="优化召回",
            outcome="接入统一检索",
            decisions=[{"what": "保留原始回溯入口", "by": "user"}],
            entities=[{"name": "PostgreSQL", "type": "database"}],
            source_hash="hash-2",
        ),
    ]

    payload = _build_epoch_checkpoint_payload(epoch_input, summaries, 1)

    assert payload["current_goal"] == "优化召回"
    assert [item["description"] for item in payload["completed_milestones"]] == [
        "完成基础压缩",
        "接入统一检索",
    ]
    assert payload["current_decisions"] == [
        {"what": "保留原始回溯入口", "by": "user"},
    ]
    assert [item["name"] for item in payload["relevant_entities"]] == [
        "AIive",
        "PostgreSQL",
    ]
    assert payload["source_hashes"] == ["hash-1", "hash-2"]


def test_checkpoint_index_projection_keeps_thread_and_semantic_fields():
    checkpoint = EpochCheckpoint(
        id="checkpoint-1",
        epoch_id="epoch-1",
        current_goal="长期上下文",
        completed_milestones=[{"description": "完成压缩"}],
        current_decisions=[{"what": "支持原始回溯"}],
        relevant_entities=[{"name": "AIive"}],
        source_hashes=["hash-1"],
        version=1,
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
        thread_id="thread-local",
    )

    fields = build_entry_fields(db, "epoch_checkpoint", checkpoint)

    assert fields is not None
    assert fields["thread_id"] == "thread-local"
    assert "完成压缩" in fields["search_text"]
    assert "支持原始回溯" in fields["search_text"]
    assert "AIive" in fields["search_text"]


def test_history_index_route_is_fail_closed_to_current_thread():
    local = RetrievalIndexEntry(
        id="entry-local",
        source_type="segment_summary",
        source_id="summary-local",
        source_version="1",
        index_version=1,
        thread_id="thread-local",
        lifecycle_state="valid",
        title="本地摘要",
        snippet="本地内容",
    )
    foreign = RetrievalIndexEntry(
        id="entry-foreign",
        source_type="segment_summary",
        source_id="summary-foreign",
        source_version="1",
        index_version=1,
        thread_id="thread-other",
        lifecycle_state="valid",
        title="其他线程摘要",
        snippet="不应出现",
    )
    legacy_without_thread = RetrievalIndexEntry(
        id="entry-legacy",
        source_type="epoch_checkpoint",
        source_id="checkpoint-legacy",
        source_version="1",
        index_version=1,
        thread_id=None,
        lifecycle_state="valid",
        title="旧索引",
        snippet="等待重建",
    )
    retriever = UnifiedRetriever(MagicMock())
    retriever._index = MagicMock()  # pyright: ignore[reportPrivateUsage]
    retriever._index.get_active_generation.return_value = SimpleNamespace(index_version=1)
    retriever._index.query_tokens.return_value = [
        ("entry-local", 2),
        ("entry-foreign", 2),
        ("entry-legacy", 1),
    ]
    retriever._index.fetch_entries_by_ids.return_value = [
        local,
        foreign,
        legacy_without_thread,
    ]

    hits = retriever._index_route(  # pyright: ignore[reportPrivateUsage]
        RetrievalRequest(
            query="摘要",
            mode=RetrievalMode.AUTO,
            thread_id="thread-local",
            source_types=["segment_summary", "epoch_checkpoint"],
        ),
        include_sleeping=False,
        top_k=8,
    )

    assert [hit.source_id for hit in hits] == ["summary-local"]
