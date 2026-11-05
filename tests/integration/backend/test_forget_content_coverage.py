"""Forget 宽范围内容清理矩阵：SQLite 真实 _scrub_wide -> 对象删除 -> Verify 链路。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session, sessionmaker

from aiive.db.forget_models import (
    ForgetAction,
    ForgetOperation,
    ForgetSelectorManifest,
    ForgetShield,
)
from aiive.db.models import (
    AgentAction,
    AgentRun,
    AgentTask,
    AgentTaskCheckpoint,
    AgentTaskEvent,
    AgentTaskWatch,
    Artifact,
    AttentionState,
    ApprovalRequest,
    Chunk,
    CompactionInput,
    ContextSnapshot,
    CoreMemoryBlock,
    DesktopActionReceipt,
    Document,
    Epoch,
    EpochCheckpoint,
    EpochCompactionInput,
    Event,
    KnowledgeChunkVector,
    LLMCall,
    RetrievalIndexEntry,
    RetrievalIndexToken,
    MemoryConflictCase,
    MemoryEvidence,
    MemoryMaintenanceAction,
    MemoryMaintenanceInput,
    MemoryProposal,
    MemoryRecallRun,
    OutboxJob,
    RetrievalRun,
    MemoryRecord,
    Segment,
    SegmentSummary,
    Task,
    TaskArtifact,
    ToolOperation,
    TaskEvidence,
    Thread,
    TurnEventInbox,
    TurnRecord,
    WorkingState,
)
from aiive.forget import content_scrub
from aiive.forget.content_scrub import (
    process_object_deletions,
    scrub_all,
    verify_scope,
)
from aiive.forget.content_scope import load_scope
from aiive.storage import object_store
from aiive.worker.handlers_forget import _scrub_wide


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _thread(db: Session) -> str:
    tid = _id()
    db.add(Thread(id=tid))
    db.flush()
    return tid


def _op(
    db: Session, *, mode: str, selector_type: str, payload: dict,
) -> str:
    # 先 flush 让既有内容拿到真实 created_at，再冻结 Phase A cutoff（模拟
    # "数据先存在、Forget 请求后到达" 的真实时序）。
    db.flush()
    op_id = _id()
    db.add(ForgetOperation(
        id=op_id, operation_key=f"forget:{op_id}", mode=mode,
        selector_type=selector_type, selector_hash="h", status="shielded",
        requested_by="test", target_count=0, shielded_at=_now(),
    ))
    db.add(ForgetSelectorManifest(
        id=_id(), forget_operation_id=op_id, selector_type=selector_type,
        selector_payload={"selector_type": selector_type, **payload},
        selector_payload_hash="h", cutoff_created_at=_now(),
    ))
    db.flush()
    return op_id


def _event(db: Session, thread_id: str, text: str = "secret") -> str:
    eid = _id()
    db.add(Event(
        id=eid, trace_id=_id(), thread_id=thread_id, event_type="user_message",
        payload={"text": text}, created_at=_now(),
    ))
    db.flush()
    return eid


def _turn(db: Session, thread_id: str, turn_id: str | None = None) -> TurnRecord:
    rec = TurnRecord(
        id=_id(), thread_id=thread_id, turn_id=turn_id or _id(),
        turn_sequence=1, status="completed", request_fingerprint="f",
        response_payload={"text": "turn secret"}, source="chat", created_at=_now(),
    )
    db.add(rec)
    db.flush()
    return rec


def _object(tmp_path: Path, data: bytes = b"sensitive-object") -> tuple[str, str]:
    ref, _hash = object_store.put_content_addressed("task", data)
    return ref.bucket, ref.key


@pytest.fixture(autouse=True)
def _isolated_object_store(tmp_path, monkeypatch):
    from aiive.tools import safe_delete as safe_delete_module

    root = tmp_path / "object_store"
    registry = safe_delete_module.SafeDeleteScopeRegistry()
    registry.register("object_store", root)
    monkeypatch.setattr(object_store, "ROOT", root)
    monkeypatch.setattr(safe_delete_module, "_default_registry", registry)
    return root


def test_thread_scrub_covers_runtime_task_and_summary_tables(
    db_session: Session,
) -> None:
    thread_id = _thread(db_session)
    ev = _event(db_session, thread_id)
    turn = _turn(db_session, thread_id)
    seg = Segment(
        id=_id(), epoch_id=_id(), thread_id=thread_id, segment_no=1,
        status="open", start_turn_sequence=1, end_turn_sequence=1,
        created_at=_now(),
    )
    db_session.add(seg)
    db_session.add(Epoch(
        id=seg.epoch_id, thread_id=thread_id, epoch_no=1, status="open",
        created_at=_now(),
    ))
    db_session.add(SegmentSummary(
        id=_id(), segment_id=seg.id, goal="goal", outcome="outcome",
        decisions=["d"], open_loops=["o"], entities=["e"], artifacts=["a"],
        important_tool_results=["r"], active_constraints=["c"],
        unresolved_failures=["u"], omitted_artifact_refs=["x"],
        source_event_ids=[ev], source_turn_ids=[turn.turn_id],
        source_hash="h", summary_version=1, token_count=10, created_at=_now(),
    ))
    db_session.add(EpochCheckpoint(
        id=_id(), epoch_id=seg.epoch_id, current_goal="cg",
        completed_milestones=["m"], open_loops=["o"], active_constraints=["c"],
        current_decisions=["d"], referenced_artifacts=["a"],
        relevant_entities=["e"], latest_verified_tool_states=["v"],
        source_segment_ids=[seg.id], source_hashes=["h"], version=1,
        token_count=1, created_at=_now(),
    ))
    db_session.add(WorkingState(
        id=_id(), thread_id=thread_id, current_objective="obj",
        open_loops=["o"], active_constraints=["c"], pending_approvals=["p"],
        artifact_refs=["a"], verified_tool_states=["v"],
        uncommitted_side_effects=["u"], running_tool_state=["r"],
        applied_idempotency_keys=["k"],
    ))
    db_session.add(ContextSnapshot(
        id=_id(), trace_id=_id(), thread_id=thread_id, stable_prefix_hash="h",
        context_items=[{"text": "ctx"}], meta={"m": 1}, token_total=1,
        created_at=_now(),
    ))
    db_session.add(LLMCall(
        id=_id(), trace_id=_id(), thread_id=thread_id, model="m",
        latency_ms=1.0, input_preview="in", output_preview="out",
        created_at=_now(),
    ))
    db_session.add(Task(
        id=_id(), task_type="reminder", status="pending", title="title",
        description="desc", thread_id=thread_id, condition="cond",
        created_at=_now(),
    ))
    db_session.add(Artifact(
        id=_id(), thread_id=thread_id, kind="text", ref="r", content="artifact",
        content_hash="h", token_count=1, turn_record_id=turn.id, created_at=_now(),
    ))
    db_session.add(AttentionState(
        id=_id(), thread_id=thread_id, focus_topic="topic",
        recent_topics=["a"], decision="none", created_at=_now(),
    ))
    db_session.add(TurnEventInbox(
        id=_id(), thread_id=thread_id, dedupe_key="d", event_type="e",
        source="s", priority=1, payload={"text": "inbox"}, status="pending",
        created_at=_now(),
    ))
    db_session.add(CompactionInput(
        id=_id(), segment_id=seg.id, start_turn_sequence=1, end_turn_sequence=1,
        turn_manifest=[{"t": 1}], event_manifest=[{"e": 1}],
        working_state_version=1, working_state_snapshot={"x": 1},
        source_hash="h", summary_version=1, created_at=_now(),
    ))
    db_session.add(EpochCompactionInput(
        id=_id(), epoch_id=seg.epoch_id, boundary_turn_sequence=1,
        working_state_version=1, current_objective="obj",
        open_loops=["o"], active_constraints=["c"], artifact_refs=["a"],
        verified_tool_states=["v"], source_segment_ids=[seg.id],
        source_hashes=["h"], snapshot_hash="h", checkpoint_version=1,
        created_at=_now(),
    ))

    agent_task = AgentTask(
        id=_id(), thread_id=thread_id, task_type="research", title="task title",
        goal="task goal", status="queued", executor_type="llm", priority=0,
        task_brief={"b": "brief"}, task_state={"s": "state"},
        report={"r": "report"}, last_error="boom", created_at=_now(),
    )
    db_session.add(agent_task)
    db_session.add(AgentRun(
        id=_id(), task_id=agent_task.id, run_index=0, trigger="user",
        status="completed", input_snapshot={"i": "input"},
        output_summary={"o": "output"}, error_message="err", created_at=_now(),
    ))
    db_session.add(AgentAction(
        id=_id(), task_id=agent_task.id, run_id=_id(), sequence=1,
        capability_id="cap", arguments={"a": 1}, arguments_hash="h",
        descriptor_hash="h", idempotency_key="k", status="succeeded",
        risk_level="low", requires_approval=False, preconditions=[{"p": 1}],
        effects=[{"e": 1}], result_summary={"r": 1}, evidence_refs=[{"x": 1}],
        error_message="err", created_at=_now(),
    ))
    db_session.add(AgentTaskEvent(
        id=_id(), task_id=agent_task.id, sequence=1, event_type="e",
        visibility="conversation", payload={"text": "agent event"}, created_at=_now(),
    ))
    db_session.add(AgentTaskCheckpoint(
        id=_id(), task_id=agent_task.id, version=1, event_sequence=1,
        state={"s": 1}, plan={"p": 1}, pending_action_refs=[{"a": 1}],
        evidence_refs=[{"e": 1}], reason="r", snapshot_hash="h",
        created_at=_now(),
    ))
    # thread 选择器不得无依据删除独立 Knowledge 文档。
    doc = Document(
        id=_id(), source_path="external/user.txt", content_hash="h",
        mime_type="text/plain", title="doc", doc_type="text", status="indexed",
        parser_version="p", chunker_version="c", created_at=_now(),
    )
    db_session.add(doc)

    op_id = _op(db_session, mode="history_only", selector_type="thread",
                payload={"thread_id": thread_id})
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()

    assert db_session.query(Event).filter_by(id=ev).first().payload == {}
    assert db_session.query(TurnRecord).filter_by(id=turn.id).first().response_payload is None
    ws = db_session.query(WorkingState).filter_by(thread_id=thread_id).first()
    assert ws.current_objective is None and ws.open_loops == []
    assert db_session.query(ContextSnapshot).first().context_items == []
    assert db_session.query(LLMCall).first().input_preview == ""
    assert db_session.query(Task).first().description == ""
    assert db_session.query(Artifact).first().content == "[redacted]"
    assert db_session.query(TurnEventInbox).first().payload == {}
    summary = db_session.query(SegmentSummary).first()
    assert summary.goal == "[redacted]" and summary.decisions == []
    assert summary.source_event_ids == [ev]  # 审计来源保留
    checkpoint = db_session.query(EpochCheckpoint).first()
    assert checkpoint.current_goal == "[redacted]" and checkpoint.completed_milestones == []
    assert db_session.query(CompactionInput).first().working_state_snapshot == {}
    assert db_session.query(EpochCompactionInput).first().current_objective is None
    task = db_session.query(AgentTask).filter_by(id=agent_task.id).first()
    assert task.title == "[redacted]" and task.goal == "[redacted]"
    assert task.report is None and task.last_error is None
    assert db_session.query(AgentRun).first().input_snapshot == {}
    assert db_session.query(AgentRun).first().output_summary is None
    assert db_session.query(AgentRun).first().error_message is None
    action = db_session.query(AgentAction).first()
    assert action.arguments == {} and action.result_summary is None
    assert action.preconditions == [] and action.effects == []
    assert action.evidence_refs == [] and action.error_message is None
    assert db_session.query(AgentTaskEvent).first().payload == {}
    checkpoint = db_session.query(AgentTaskCheckpoint).first()
    assert checkpoint.state == {} and checkpoint.plan == {}
    assert checkpoint.pending_action_refs == [] and checkpoint.evidence_refs == []
    assert db_session.query(Document).filter_by(id=doc.id).first().status == "indexed"


def test_canonical_key_does_not_touch_unrelated_content(db_session: Session) -> None:
    thread_id = _thread(db_session)
    ev_target = _event(db_session, thread_id, "target")
    ev_other = _event(db_session, thread_id, "other")
    m_target = MemoryRecord(
        id=_id(), memory_type="fact", canonical_key="k:target", content="secret",
        lifecycle_state="active", validity_state="valid", confidence=0.9,
        created_at=_now(), source_event_id=ev_target,
    )
    m_other = MemoryRecord(
        id=_id(), memory_type="fact", canonical_key="k:other", content="keep",
        lifecycle_state="active", validity_state="valid", confidence=0.9,
        created_at=_now(), source_event_id=ev_other,
    )
    db_session.add_all([m_target, m_other])
    db_session.add(MemoryProposal(
        id=_id(), proposal_id="p1", source_event_ids=[ev_target],
        canonical_key="k:target", content="prop secret",
        structured_value={"v": 1}, evidence=[{"span": "s"}],
        raw_payload={"raw": 1}, normalized_payload={"n": 1},
        final_memory_id=m_target.id, execution_mode="sync", retention_policy="standard",
        durable=True, created_at=_now(),
    ))
    db_session.add(MemoryEvidence(
        id=_id(), memory_id=m_target.id, source_event_id=ev_target,
        source_type="user_assertion", trust_level="trusted", relation="support",
        content_span="span secret", created_at=_now(),
    ))
    from aiive.db.forget_models import ForgetTarget
    op_id = _op(db_session, mode="everywhere", selector_type="canonical_key",
                payload={"canonical_key": "k:target"})
    db_session.add(ForgetTarget(
        id=_id(), forget_operation_id=op_id, target_type="memory_record",
        target_id=m_target.id, batch_no=0, frozen_at=_now(),
    ))
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()

    # 目标 memory 的独占来源 Event 属于可证明关联内容，按 everywhere 清理；
    # 同 Thread 的无关 Event 不得被波及。
    assert db_session.query(Event).filter_by(id=ev_target).first().payload == {}
    assert db_session.query(Event).filter_by(id=ev_other).first().payload == {"text": "other"}
    assert db_session.query(MemoryRecord).filter_by(id=m_target.id).first().content == "[forgotten]"
    assert db_session.query(MemoryRecord).filter_by(id=m_other.id).first().content == "keep"
    proposal = db_session.query(MemoryProposal).first()
    assert proposal.content is None and proposal.structured_value is None
    assert proposal.evidence == [] and proposal.raw_payload is None
    assert proposal.normalized_payload is None


def test_task_object_deleted_only_after_db_commit_and_respects_sharing(
    db_session: Session, tmp_path: Path,
) -> None:
    thread_id = _thread(db_session)
    other_thread = _thread(db_session)
    bucket, key = _object(tmp_path)
    shared_bucket, shared_key = _object(tmp_path, b"shared")

    task = AgentTask(
        id=_id(), thread_id=thread_id, task_type="research", title="t", goal="g",
        status="queued", executor_type="llm", priority=0, task_brief={},
        task_state={}, created_at=_now(),
    )
    other_task = AgentTask(
        id=_id(), thread_id=other_thread, task_type="research", title="t2", goal="g2",
        status="queued", executor_type="llm", priority=0, task_brief={},
        task_state={}, created_at=_now(),
    )
    db_session.add_all([task, other_task])
    db_session.add(TaskEvidence(
        id=_id(), task_id=task.id, kind="file", summary="summary",
        inline_payload={"x": 1}, object_bucket=bucket, object_key=key,
        content_hash="h", mime_type="text/plain", content_size=1, created_at=_now(),
    ))
    db_session.add(TaskArtifact(
        id=_id(), task_id=task.id, name="name", artifact_type="file",
        summary="summary", object_bucket=bucket, object_key=key,
        content_hash="h", mime_type="text/plain", content_size=1, created_at=_now(),
    ))
    # 非目标记录共享同一个对象 -> 不得删除。
    db_session.add(TaskEvidence(
        id=_id(), task_id=other_task.id, kind="file", summary="other",
        object_bucket=shared_bucket, object_key=shared_key,
        content_hash="h", mime_type="text/plain", content_size=1, created_at=_now(),
    ))
    db_session.add(TaskEvidence(
        id=_id(), task_id=task.id, kind="file", summary="target-shared",
        object_bucket=shared_bucket, object_key=shared_key,
        content_hash="h", mime_type="text/plain", content_size=1, created_at=_now(),
    ))
    op_id = _op(db_session, mode="everywhere", selector_type="thread",
                payload={"thread_id": thread_id})
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()
    # 提交前不得删除对象。
    assert object_store.exists(object_store.ObjectRef(bucket, key))
    assert db_session.query(TaskEvidence).filter_by(task_id=task.id).first().object_key is None

    result = process_object_deletions(db_session, op_id)
    assert result["failed"] == []
    assert not object_store.exists(object_store.ObjectRef(bucket, key))
    assert object_store.exists(object_store.ObjectRef(shared_bucket, shared_key))


def test_knowledge_scrub_deletes_derived_and_object_but_not_source_file(
    db_session: Session, tmp_path: Path,
) -> None:
    source = tmp_path / "user-original.txt"
    source.write_text("user file", encoding="utf-8")
    bucket, key = _object(tmp_path, b"doc original")
    doc = Document(
        id=_id(), source_path=str(source), content_hash="h",
        object_bucket=bucket, object_key=key, content_size=1, mime_type="text/plain",
        title="title", doc_type="text", status="indexed", parser_version="p",
        chunker_version="c", created_at=_now(),
    )
    db_session.add(doc)
    db_session.add(Chunk(
        id=_id(), document_id=doc.id, content="chunk", line_start=0, line_end=1,
        chunk_index=0, token_estimate=1, heading_path="", section_id="",
        block_type="text", source_type="text", parser_version="p",
        chunker_version="c", embedding_input_hash="h", created_at=_now(),
    ))
    db_session.flush()
    chunk = db_session.query(Chunk).first()
    db_session.add(KnowledgeChunkVector(
        chunk_id=chunk.id, content_hash="h", embedding_model="m",
        input_version="1", embedding=[0.0] * 512, created_at=_now(),
    ))
    op_id = _op(db_session, mode="everywhere", selector_type="all_user_data",
                payload={"all_user_data": True})
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()
    assert db_session.query(Chunk).count() == 0
    assert db_session.query(KnowledgeChunkVector).count() == 0
    assert object_store.exists(object_store.ObjectRef(bucket, key))

    result = process_object_deletions(db_session, op_id)
    assert result["failed"] == []
    assert not object_store.exists(object_store.ObjectRef(bucket, key))
    assert source.read_text(encoding="utf-8") == "user file"
    db_session.expire_all()
    assert db_session.query(Document).first().status == "deleted"


def test_memory_only_preserves_raw_history_but_scrubs_derived(
    db_session: Session,
) -> None:
    thread_id = _thread(db_session)
    ev = _event(db_session, thread_id)
    turn = _turn(db_session, thread_id)
    db_session.add(WorkingState(id=_id(), thread_id=thread_id, current_objective="obj"))
    op_id = _op(db_session, mode="memory_only", selector_type="thread",
                payload={"thread_id": thread_id})
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()

    assert db_session.query(Event).filter_by(id=ev).first().payload == {"text": "secret"}
    assert db_session.query(TurnRecord).filter_by(id=turn.id).first().response_payload is not None
    assert db_session.query(WorkingState).first().current_objective is None


def test_verify_detects_residue_and_keeps_shield_active(db_session: Session) -> None:
    thread_id = _thread(db_session)
    _event(db_session, thread_id)
    op_id = _op(db_session, mode="everywhere", selector_type="thread",
                payload={"thread_id": thread_id})
    db_session.add(ForgetShield(
        id=_id(), forget_operation_id=op_id, selector_type="thread",
        thread_id=thread_id, cutoff_created_at=_now(), status="active",
        normalized_shield_key=f"shield:{op_id}", created_at=_now(),
    ))
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()
    scope = load_scope(db_session, op_id, "everywhere")
    assert scope is not None
    report = verify_scope(db_session, scope)
    assert report.ok

    # 人为制造残留：Verify 必须重新读库并发现。
    db_session.execute(
        sa_text("UPDATE events SET payload = '{\"text\": \"leak\"}' WHERE thread_id = :tid"),
        {"tid": thread_id},
    )
    db_session.flush()
    report = verify_scope(db_session, scope)
    assert not report.ok
    assert report.residues.get("events.payload", 0) >= 1


def _at(delta_seconds: int) -> datetime:
    from datetime import timedelta
    return datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)


def test_cutoff_keeps_post_request_data_but_cleans_old_derived(
    db_session: Session,
) -> None:
    thread_id = _thread(db_session)
    old_ev = _event(db_session, thread_id, "old secret")
    old_memory = MemoryRecord(
        id=_id(), memory_type="fact", canonical_key="k:old", scope_type="thread",
        scope_id=thread_id, content="old memory", lifecycle_state="active",
        validity_state="valid", confidence=0.9, created_at=_now(),
        source_event_id=old_ev,
    )
    db_session.add(old_memory)
    op_id = _op(db_session, mode="everywhere", selector_type="thread",
                payload={"thread_id": thread_id})
    from aiive.db.forget_models import ForgetTarget
    db_session.add(ForgetTarget(
        id=_id(), forget_operation_id=op_id, target_type="memory_record",
        target_id=old_memory.id, batch_no=0, frozen_at=_now(),
    ))

    # Phase A 之后：同 Thread 的新 Event / Memory / Task / Summary。
    new_ev = _event(db_session, thread_id, "new secret")
    new_memory = MemoryRecord(
        id=_id(), memory_type="fact", canonical_key="k:new", scope_type="thread",
        scope_id=thread_id, content="new memory", lifecycle_state="active",
        validity_state="valid", confidence=0.9, created_at=_at(60),
        source_event_id=new_ev,
    )
    new_task = AgentTask(
        id=_id(), thread_id=thread_id, task_type="research", title="new task",
        goal="new goal", status="queued", executor_type="llm", priority=0,
        task_brief={"x": 1}, task_state={"y": 1}, created_at=_at(60),
    )
    db_session.add_all([new_memory, new_task])
    # 派生记录：cutoff 之后创建，但来源是旧 Event → 必须清理。
    seg_old = Segment(
        id=_id(), epoch_id=_id(), thread_id=thread_id, segment_no=1, status="open",
        start_turn_sequence=1, end_turn_sequence=1, created_at=_at(60),
    )
    db_session.add(seg_old)
    db_session.add(Epoch(
        id=seg_old.epoch_id, thread_id=thread_id, epoch_no=1, status="open",
        created_at=_at(60),
    ))
    db_session.add(SegmentSummary(
        id=_id(), segment_id=seg_old.id, goal="old-derived", outcome="o",
        source_event_ids=[old_ev], summary_version=1, token_count=1,
        created_at=_at(60),
    ))
    db_session.add(SegmentSummary(
        id=_id(), segment_id=_id(), goal="new-derived", outcome="o",
        source_event_ids=[new_ev], summary_version=1, token_count=1,
        created_at=_at(60),
    ))
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()

    assert db_session.query(Event).filter_by(id=old_ev).first().payload == {}
    assert db_session.query(Event).filter_by(id=new_ev).first().payload == {"text": "new secret"}
    assert db_session.query(MemoryRecord).filter_by(id=old_memory.id).first().content == "[forgotten]"
    assert db_session.query(MemoryRecord).filter_by(id=new_memory.id).first().content == "new memory"
    assert db_session.query(AgentTask).filter_by(id=new_task.id).first().title == "new task"
    goals = {r.goal for r in db_session.query(SegmentSummary).all()}
    assert "[redacted]" in goals and "new-derived" in goals


def test_time_range_uses_each_table_real_time_column(db_session: Session) -> None:
    inside = _thread(db_session)
    outside = _thread(db_session)
    ev_in = _event(db_session, inside, "inside")
    ev_out = _event(db_session, outside, "outside")
    db_session.query(Event).filter_by(id=ev_in).update({"created_at": _at(-60)})
    db_session.query(Event).filter_by(id=ev_out).update({"created_at": _at(3600)})

    turn_in = _turn(db_session, inside)
    turn_out = _turn(db_session, outside)
    db_session.query(TurnRecord).filter_by(id=turn_in.id).update({"created_at": _at(-60)})
    db_session.query(TurnRecord).filter_by(id=turn_out.id).update({"created_at": _at(3600)})

    ws_in = WorkingState(id=_id(), thread_id=inside, current_objective="inside",
                         updated_at=_at(-60))
    ws_out = WorkingState(id=_id(), thread_id=outside, current_objective="outside",
                          updated_at=_at(3600))
    db_session.add_all([ws_in, ws_out])
    db_session.add(ContextSnapshot(
        id=_id(), trace_id=_id(), thread_id=inside, stable_prefix_hash="h",
        context_items=[{"x": 1}], token_total=1, created_at=_at(-60),
    ))
    db_session.add(ContextSnapshot(
        id=_id(), trace_id=_id(), thread_id=outside, stable_prefix_hash="h",
        context_items=[{"x": 2}], token_total=1, created_at=_at(3600),
    ))
    db_session.add(LLMCall(
        id=_id(), trace_id=_id(), thread_id=inside, model="m", latency_ms=1.0,
        input_preview="inside", created_at=_at(-60),
    ))
    db_session.add(LLMCall(
        id=_id(), trace_id=_id(), thread_id=outside, model="m", latency_ms=1.0,
        input_preview="outside", created_at=_at(3600),
    ))
    db_session.add(Task(
        id=_id(), task_type="reminder", status="pending", title="inside",
        description="inside", thread_id=inside, created_at=_at(-60),
    ))
    db_session.add(Task(
        id=_id(), task_type="reminder", status="pending", title="outside",
        description="outside", thread_id=outside, created_at=_at(3600),
    ))
    task_in = AgentTask(
        id=_id(), thread_id=inside, task_type="research", title="inside", goal="g",
        status="queued", executor_type="llm", priority=0, task_brief={},
        task_state={}, created_at=_at(-60),
    )
    task_out = AgentTask(
        id=_id(), thread_id=outside, task_type="research", title="outside", goal="g",
        status="queued", executor_type="llm", priority=0, task_brief={},
        task_state={}, created_at=_at(3600),
    )
    db_session.add_all([task_in, task_out])
    seg_in = Segment(
        id=_id(), epoch_id=_id(), thread_id=inside, segment_no=1, status="open",
        start_turn_sequence=1, end_turn_sequence=1, created_at=_at(-60),
    )
    seg_out = Segment(
        id=_id(), epoch_id=_id(), thread_id=outside, segment_no=1, status="open",
        start_turn_sequence=1, end_turn_sequence=1, created_at=_at(3600),
    )
    db_session.add_all([seg_in, seg_out])
    db_session.add(SegmentSummary(
        id=_id(), segment_id=seg_in.id, goal="inside", outcome="o",
        source_event_ids=[ev_in], summary_version=1, token_count=1,
        created_at=_at(-60),
    ))
    db_session.add(SegmentSummary(
        id=_id(), segment_id=seg_out.id, goal="outside", outcome="o",
        source_event_ids=[ev_out], summary_version=1, token_count=1,
        created_at=_at(3600),
    ))
    op_id = _op(
        db_session, mode="everywhere", selector_type="time_range",
        payload={"time_from": _at(-120).isoformat(), "time_to": _at(0).isoformat()},
    )
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()

    assert db_session.query(Event).filter_by(id=ev_in).first().payload == {}
    assert db_session.query(Event).filter_by(id=ev_out).first().payload == {"text": "outside"}
    assert db_session.query(TurnRecord).filter_by(id=turn_in.id).first().response_payload is None
    assert db_session.query(TurnRecord).filter_by(id=turn_out.id).first().response_payload is not None
    assert db_session.query(WorkingState).filter_by(id=ws_in.id).first().current_objective is None
    assert db_session.query(WorkingState).filter_by(id=ws_out.id).first().current_objective == "outside"
    snapshots = {r.id: r.context_items for r in db_session.query(ContextSnapshot).all()}
    assert [] in snapshots.values() and [{"x": 2}] in snapshots.values()
    calls = {r.id: r.input_preview for r in db_session.query(LLMCall).all()}
    assert "" in calls.values() and "outside" in calls.values()
    tasks = {r.id: r.description for r in db_session.query(Task).all()}
    assert "" in tasks.values() and "outside" in tasks.values()
    agent_titles = {r.id: r.title for r in db_session.query(AgentTask).all()}
    assert "[redacted]" in agent_titles.values() and "outside" in agent_titles.values()
    summary_goals = {r.segment_id: r.goal for r in db_session.query(SegmentSummary).all()}
    assert summary_goals[seg_in.id] == "[redacted]"
    assert summary_goals[seg_out.id] == "outside"


def test_scope_selector_does_not_touch_unrelated_thread(db_session: Session) -> None:
    thread_id = _thread(db_session)
    other_thread = _thread(db_session)
    ev = _event(db_session, thread_id, "target")
    other_ev = _event(db_session, other_thread, "other")
    m_target = MemoryRecord(
        id=_id(), memory_type="fact", canonical_key="k", scope_type="thread",
        scope_id=thread_id, content="secret", lifecycle_state="active",
        validity_state="valid", confidence=0.9, created_at=_now(),
        source_event_id=ev,
    )
    db_session.add(m_target)
    db_session.add(WorkingState(id=_id(), thread_id=thread_id, current_objective="obj"))
    db_session.add(WorkingState(id=_id(), thread_id=other_thread, current_objective="keep"))
    from aiive.db.forget_models import ForgetTarget
    op_id = _op(db_session, mode="everywhere", selector_type="scope",
                payload={"scope_type": "thread", "scope_id": thread_id})
    db_session.add(ForgetTarget(
        id=_id(), forget_operation_id=op_id, target_type="memory_record",
        target_id=m_target.id, batch_no=0, frozen_at=_now(),
    ))
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()

    assert db_session.query(Event).filter_by(id=other_ev).first().payload == {"text": "other"}
    ws = {r.thread_id: r.current_objective for r in db_session.query(WorkingState).all()}
    assert ws[thread_id] == "obj"    # scope 不清理无关运行态
    assert ws[other_thread] == "keep"
    assert db_session.query(MemoryRecord).filter_by(id=m_target.id).first().content == "[forgotten]"


def test_object_delete_failure_is_retryable(
    db_session: Session, tmp_path: Path, monkeypatch,
) -> None:
    thread_id = _thread(db_session)
    bucket, key = _object(tmp_path, b"retry-object")
    task = AgentTask(
        id=_id(), thread_id=thread_id, task_type="research", title="t", goal="g",
        status="queued", executor_type="llm", priority=0, task_brief={},
        task_state={}, created_at=_now(),
    )
    db_session.add(task)
    db_session.add(TaskEvidence(
        id=_id(), task_id=task.id, kind="file", summary="s",
        object_bucket=bucket, object_key=key, content_hash="h",
        mime_type="text/plain", content_size=1, created_at=_now(),
    ))
    op_id = _op(db_session, mode="everywhere", selector_type="thread",
                payload={"thread_id": thread_id})
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()

    original_delete = object_store.delete

    def boom(ref):
        raise RuntimeError("simulated object store failure")

    monkeypatch.setattr(object_store, "delete", boom)
    first = process_object_deletions(db_session, op_id)
    assert first["failed"]
    assert object_store.exists(object_store.ObjectRef(bucket, key))
    action = db_session.query(ForgetAction).filter_by(
        forget_operation_id=op_id, action_type="delete_object",
    ).first()
    assert action is not None and action.status == "failed"

    monkeypatch.setattr(object_store, "delete", original_delete)
    second = process_object_deletions(db_session, op_id)
    assert second["failed"] == []
    assert not object_store.exists(object_store.ObjectRef(bucket, key))
    db_session.refresh(action)
    assert action.status == "done"


def test_two_target_records_sharing_object_delete_once(
    db_session: Session, tmp_path: Path,
) -> None:
    thread_id = _thread(db_session)
    bucket, key = _object(tmp_path, b"shared-target-object")
    tasks = [
        AgentTask(
            id=_id(), thread_id=thread_id, task_type="research", title=f"t{i}",
            goal="g", status="queued", executor_type="llm", priority=0,
            task_brief={}, task_state={}, created_at=_now(),
        )
        for i in range(2)
    ]
    db_session.add_all(tasks)
    for task in tasks:
        db_session.add(TaskEvidence(
            id=_id(), task_id=task.id, kind="file", summary="s",
            object_bucket=bucket, object_key=key, content_hash="h",
            mime_type="text/plain", content_size=1, created_at=_now(),
        ))
    op_id = _op(db_session, mode="everywhere", selector_type="thread",
                payload={"thread_id": thread_id})
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()
    actions = db_session.query(ForgetAction).filter_by(
        forget_operation_id=op_id, action_type="delete_object",
    ).all()
    assert len(actions) == 1  # 同一对象只登记一次
    result = process_object_deletions(db_session, op_id)
    assert result["failed"] == []
    assert not object_store.exists(object_store.ObjectRef(bucket, key))


def test_all_user_scrubs_task_runtime_and_knowledge(db_session: Session) -> None:
    first = _thread(db_session)
    second = _thread(db_session)
    for thread_id, title in ((first, "one"), (second, "two")):
        db_session.add(AgentTask(
            id=_id(), thread_id=thread_id, task_type="research", title=title,
            goal="goal", status="queued", executor_type="llm", priority=0,
            task_brief={"b": 1}, task_state={"s": 1}, created_at=_now(),
        ))
    doc = Document(
        id=_id(), source_path="external/a.txt", content_hash="h",
        mime_type="text/plain", title="doc", doc_type="text", status="indexed",
        parser_version="p", chunker_version="c", created_at=_now(),
    )
    db_session.add(doc)
    db_session.add(Chunk(
        id=_id(), document_id=doc.id, content="chunk", line_start=0, line_end=1,
        chunk_index=0, token_estimate=1, heading_path="", section_id="",
        block_type="text", source_type="text", parser_version="p",
        chunker_version="c", embedding_input_hash="h", created_at=_now(),
    ))
    op_id = _op(db_session, mode="everywhere", selector_type="all_user_data",
                payload={"all_user_data": True})
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()

    assert {
        row.title for row in db_session.query(AgentTask).all()
    } == {"[redacted]"}
    assert {
        row.title for row in db_session.query(Document).all()
    } == {"[redacted]"}
    assert db_session.query(Chunk).count() == 0


def test_turn_selector_scrubs_derived_summary_by_json_membership(
    db_session: Session,
) -> None:
    thread_id = _thread(db_session)
    turn = _turn(db_session, thread_id)
    seg = Segment(
        id=_id(), epoch_id=_id(), thread_id=thread_id, segment_no=1,
        status="open", start_turn_sequence=1, end_turn_sequence=1,
        created_at=_now(),
    )
    db_session.add(seg)
    db_session.add(Epoch(
        id=seg.epoch_id, thread_id=thread_id, epoch_no=1, status="open",
        created_at=_now(),
    ))
    db_session.add(SegmentSummary(
        id=_id(), segment_id=seg.id, goal="goal", outcome="outcome",
        decisions=["d"], source_turn_ids=[turn.turn_id], source_event_ids=[],
        source_hash="h", summary_version=1, token_count=1, created_at=_now(),
    ))
    from aiive.db.forget_models import ForgetTarget
    op_id = _op(db_session, mode="history_only", selector_type="turn_ids",
                payload={"turn_ids": [turn.id]})
    db_session.add(ForgetTarget(
        id=_id(), forget_operation_id=op_id, target_type="turn_record",
        target_id=turn.id, batch_no=0, frozen_at=_now(),
    ))
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()

    assert db_session.query(TurnRecord).filter_by(id=turn.id).first().response_payload is None
    summary = db_session.query(SegmentSummary).first()
    assert summary.goal == "[redacted]" and summary.decisions == []
    assert summary.source_turn_ids == [turn.turn_id]


def test_checkpoint_json_membership_sqlite(db_session: Session) -> None:
    from aiive.forget.content_scope import select_json_array_members

    db_session.add(EpochCheckpoint(
        id="c1", epoch_id="e1", current_goal="g",
        source_segment_ids=["seg-a", "seg-b"], source_hashes=["h"],
        version=1, token_count=0, created_at=_now(),
    ))
    db_session.add(EpochCheckpoint(
        id="c2", epoch_id="e2", current_goal="g",
        source_segment_ids=None, source_hashes=None,
        version=1, token_count=0, created_at=_now(),
    ))
    db_session.commit()

    assert select_json_array_members(
        db_session, "epoch_checkpoints", "id", "source_segment_ids", ["seg-b"],
    ) == ["c1"]
    assert select_json_array_members(
        db_session, "epoch_checkpoints", "id", "source_segment_ids", ["seg-x"],
    ) == []


def test_purge_object_failure_retryable_and_no_verify_enqueued(
    db_session: Session, tmp_path: Path, monkeypatch,
) -> None:
    from aiive.db.models import OutboxJob
    from aiive.worker.handlers_forget import handle_forget_purge

    thread_id = _thread(db_session)
    bucket, key = _object(tmp_path, b"purge-retry-object")
    task = AgentTask(
        id=_id(), thread_id=thread_id, task_type="research", title="t", goal="g",
        status="queued", executor_type="llm", priority=0, task_brief={},
        task_state={}, created_at=_now(),
    )
    db_session.add(task)
    db_session.add(TaskEvidence(
        id=_id(), task_id=task.id, kind="file", summary="s",
        object_bucket=bucket, object_key=key, content_hash="h",
        mime_type="text/plain", content_size=1, created_at=_now(),
    ))
    op_id = _op(db_session, mode="everywhere", selector_type="thread",
                payload={"thread_id": thread_id})
    db_session.commit()

    original_delete = object_store.delete

    def boom(ref):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(object_store, "delete", boom)
    first = _run_job(handle_forget_purge, op_id, db_session, monkeypatch)
    assert first.outcome.value == "retryable_error"
    db_session.expire_all()
    assert db_session.query(OutboxJob).filter(
        OutboxJob.job_type == "forget_verify",
    ).count() == 0

    monkeypatch.setattr(object_store, "delete", original_delete)
    second = _run_job(handle_forget_purge, op_id, db_session, monkeypatch)
    assert second.outcome.value == "completed"
    assert not object_store.exists(object_store.ObjectRef(bucket, key))
    db_session.expire_all()
    assert db_session.query(OutboxJob).filter(
        OutboxJob.job_type == "forget_verify",
    ).count() == 1


def test_cleanup_preserves_other_thread_queries_and_knowledge_search(
    db_session: Session,
) -> None:
    from aiive.knowledge.ingestor import search_chunks
    from aiive.task_runtime.repository import TaskRepository

    target_thread = _thread(db_session)
    keep_thread = _thread(db_session)
    target_task = AgentTask(
        id=_id(), thread_id=target_thread, task_type="research", title="target",
        goal="g", status="queued", executor_type="llm", priority=0,
        task_brief={}, task_state={}, created_at=_now(),
    )
    keep_task = AgentTask(
        id=_id(), thread_id=keep_thread, task_type="research", title="keep-title",
        goal="keep-goal", status="queued", executor_type="llm", priority=0,
        task_brief={}, task_state={}, created_at=_now(),
    )
    db_session.add_all([target_task, keep_task])
    doc = Document(
        id=_id(), source_path="external/keep.txt", content_hash="h",
        mime_type="text/plain", title="keep doc", doc_type="text",
        status="indexed", parser_version="p", chunker_version="c",
        created_at=_now(),
    )
    db_session.add(doc)
    db_session.add(Chunk(
        id=_id(), document_id=doc.id, content="keep-token body", line_start=0,
        line_end=1, chunk_index=0, token_estimate=1, heading_path="",
        section_id="", block_type="text", source_type="text",
        parser_version="p", chunker_version="c", embedding_input_hash="h",
        created_at=_now(),
    ))
    op_id = _op(db_session, mode="history_only", selector_type="thread",
                payload={"thread_id": target_thread})
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()

    summary = TaskRepository(db_session).summary(
        db_session.query(AgentTask).filter_by(id=keep_task.id).first()
    )
    assert summary["title"] == "keep-title"
    hits = search_chunks(db_session, "keep-token", 5)
    assert any(hit.get("chunk_id") for hit in hits)


def test_time_range_scrubs_task_children_without_erasing_parent_history(
    db_session: Session,
) -> None:
    thread_id = _thread(db_session)
    task = AgentTask(
        id=_id(), thread_id=thread_id, task_type="research", title="long task",
        goal="goal", status="running", executor_type="llm", priority=0,
        task_brief={"initial_input": "keep brief"},
        task_state={"current_action": "keep", "evidence_id": "pending"},
        created_at=_at(-3600),
    )
    db_session.add(task)
    run_old = AgentRun(
        id=_id(), task_id=task.id, run_index=0, trigger="user", status="completed",
        input_snapshot={"old": 1}, created_at=_at(-3600),
    )
    run_new = AgentRun(
        id=_id(), task_id=task.id, run_index=1, trigger="user", status="running",
        input_snapshot={"new": 1}, output_summary={"o": 1}, error_message="new err",
        created_at=_at(-60),
    )
    ev_old = TaskEvidence(
        id=_id(), task_id=task.id, kind="file", summary="old evidence",
        inline_payload={"old": 1}, content_hash="h", mime_type="text/plain",
        content_size=1, created_at=_at(-3600),
    )
    ev_new = TaskEvidence(
        id=_id(), task_id=task.id, kind="file", summary="new evidence",
        inline_payload={"new": 1}, content_hash="h", mime_type="text/plain",
        content_size=1, created_at=_at(-60),
    )
    art_old = TaskArtifact(
        id=_id(), task_id=task.id, name="old artifact", artifact_type="file",
        summary="old", object_bucket="b", object_key="old-key",
        content_hash="h", mime_type="text/plain", content_size=1, created_at=_at(-3600),
    )
    art_new = TaskArtifact(
        id=_id(), task_id=task.id, name="new artifact", artifact_type="file",
        summary="new", object_bucket="b", object_key="new-key",
        content_hash="h", mime_type="text/plain", content_size=1, created_at=_at(-60),
    )
    db_session.add_all([run_old, run_new, ev_old, ev_new, art_old, art_new])
    task.task_state = {"current_action": "keep", "evidence_id": ev_new.id}
    op_id = _op(
        db_session, mode="everywhere", selector_type="time_range",
        payload={
            "time_from": _at(-120).isoformat(),
            "time_to": _at(0).isoformat(),
        },
    )
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()

    task = db_session.query(AgentTask).filter_by(id=task.id).first()
    assert task.title == "long task" and task.goal == "goal"
    assert task.task_brief == {"initial_input": "keep brief"}
    assert task.status == "cancelled"  # 引用了被清理 evidence → 不可安全恢复
    assert task.task_state["evidence_id"] == "[forgotten]"
    assert task.task_state["current_action"] == "keep"

    old_run = db_session.query(AgentRun).filter_by(id=run_old.id).first()
    new_run = db_session.query(AgentRun).filter_by(id=run_new.id).first()
    assert old_run.input_snapshot == {"old": 1}
    assert new_run.input_snapshot == {} and new_run.output_summary is None
    assert new_run.error_message is None

    assert db_session.query(TaskEvidence).filter_by(id=ev_old.id).first().summary == "old evidence"
    new_ev = db_session.query(TaskEvidence).filter_by(id=ev_new.id).first()
    assert new_ev.summary == "[redacted]" and new_ev.inline_payload is None
    assert db_session.query(TaskArtifact).filter_by(id=art_old.id).first().name == "old artifact"
    assert db_session.query(TaskArtifact).filter_by(id=art_new.id).first().name == "[redacted]"

    process_object_deletions(db_session, op_id)
    scope = load_scope(db_session, op_id, "everywhere")
    assert scope is not None
    report = verify_scope(db_session, scope)
    assert report.ok

    # 人为注入 Task 子记录残留 → Verify 必须发现。
    new_run.error_message = "leak"
    new_ev.inline_payload = {"leak": 1}
    art_new.name = "leak"
    db_session.commit()
    report = verify_scope(db_session, scope)
    assert not report.ok
    assert report.residues.get("agent_runs.error_message", 0) >= 1
    assert report.residues.get("task_evidence.inline_payload", 0) >= 1
    assert report.residues.get("task_artifacts.name", 0) >= 1


def test_auxiliary_content_and_outbox_scrub(db_session: Session) -> None:
    thread_id = _thread(db_session)
    ev = _event(db_session, thread_id, "aux secret")
    memory = MemoryRecord(
        id=_id(), memory_type="fact", canonical_key="k:aux", scope_type="thread",
        scope_id=thread_id, content="aux memory", lifecycle_state="active",
        validity_state="valid", confidence=0.9, created_at=_now(),
    )
    db_session.add(memory)
    reminder = Task(
        id=_id(), task_type="reminder", status="pending", title="reminder",
        description="reminder desc", condition="when", thread_id=None,
        source_event_id=ev, created_at=_now(),
    )
    case = MemoryConflictCase(
        id=_id(), status="pending", thread_id=thread_id, scope_type="thread",
        scope_id=thread_id, canonical_key="k:aux", memory_type="fact",
        sensitivity="normal", old_memory_id=memory.id, old_record_version=1,
        old_content="old case", new_content="new case", proposal_id="p1",
        proposal_payload={"content": "proposal"}, verdict="conflict",
        verdict_reason="reason", evidence=[{"span": "e"}],
        graph_evidence={"g": 1}, question="which?", decision_source="user",
        decision_span="span", resolution_reason="resolution",
        idempotency_key="case-1", created_at=_now(),
    )
    ev_trace = db_session.query(Event.trace_id).filter_by(id=ev).scalar()
    recall = MemoryRecallRun(
        id=_id(), trace_id=ev_trace,
        request_query="recall query", scope_context={"scope": 1},
        routes_executed=[{"route": "lexical"}], created_at=_now(),
    )
    retrieval = RetrievalRun(
        id=_id(), trace_id=ev_trace, query="retrieval query",
        strategy="hybrid", created_at=_now(),
    )
    outbox = OutboxJob(
        id=_id(), operation_id="op-1", job_type="memory_extract", status="completed",
        payload={"content": "outbox content", "memory_id": memory.id},
        trace_id=ev_trace, error_message="outbox error", created_at=_now(),
    )
    db_session.add_all([reminder, case, recall, retrieval, outbox])
    op_id = _op(db_session, mode="everywhere", selector_type="thread",
                payload={"thread_id": thread_id})
    from aiive.db.forget_models import ForgetTarget
    db_session.add(ForgetTarget(
        id=_id(), forget_operation_id=op_id, target_type="memory_record",
        target_id=memory.id, batch_no=0, frozen_at=_now(),
    ))
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()

    case = db_session.query(MemoryConflictCase).first()
    assert case.status == "stale"
    assert case.old_content == "[redacted]" and case.new_content == "[redacted]"
    assert case.proposal_payload == {} and case.evidence == [] and case.graph_evidence == []
    assert case.question == "[redacted]" and case.verdict_reason == ""
    assert case.decision_span == "" and case.resolution_reason == ""
    assert db_session.query(MemoryRecallRun).first().request_query == "[redacted]"
    assert db_session.query(RetrievalRun).first().query == "[redacted]"
    outbox = db_session.query(OutboxJob).first()
    assert outbox.payload["content"] == "[redacted]"
    assert outbox.payload["memory_id"] == memory.id
    assert outbox.error_message == ""
    assert db_session.query(Task).filter_by(id=reminder.id).first().title == "[redacted]"

    # 人为注入残留 → Verify 必须发现。
    case.old_content = "leak"
    outbox.payload = {"content": "leak"}
    db_session.commit()
    scope = load_scope(db_session, op_id, "everywhere")
    assert scope is not None
    report = verify_scope(db_session, scope)
    assert not report.ok
    assert report.residues.get("memory_conflict_cases.old_content", 0) >= 1
    assert report.residues.get("outbox_jobs", 0) >= 1


def test_unknown_json_element_fails_closed_only_in_scope(db_session: Session) -> None:
    target_thread = _thread(db_session)
    other_thread = _thread(db_session)
    ev = _event(db_session, target_thread, "target")
    other_ev = _event(db_session, other_thread, "other")
    seg = Segment(
        id=_id(), epoch_id=_id(), thread_id=target_thread, segment_no=1,
        status="open", start_turn_sequence=1, end_turn_sequence=1, created_at=_now(),
    )
    other_seg = Segment(
        id=_id(), epoch_id=_id(), thread_id=other_thread, segment_no=1,
        status="open", start_turn_sequence=1, end_turn_sequence=1, created_at=_now(),
    )
    db_session.add_all([seg, other_seg])
    db_session.add(SegmentSummary(
        id=_id(), segment_id=seg.id, goal="target",
        source_event_ids=[{"id": ev}], summary_version=1,
        token_count=1, created_at=_now(),
    ))
    db_session.add(SegmentSummary(
        id=_id(), segment_id=other_seg.id, goal="other",
        source_event_ids=[{"id": other_ev}], summary_version=1,
        token_count=1, created_at=_now(),
    ))
    op_id = _op(db_session, mode="everywhere", selector_type="thread",
                payload={"thread_id": target_thread})
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()
    scope = load_scope(db_session, op_id, "everywhere")
    assert scope is not None
    report = verify_scope(db_session, scope)
    # 只有目标 Thread 的未知结构计入；其他 Thread 的同类数据不阻断。
    assert report.unknown == 1
    assert not report.ok


def test_every_scrubbed_field_residue_detected(db_session: Session) -> None:
    from sqlalchemy import select, update

    from aiive.db.models import Base
    from aiive.forget.content_scrub import SCRUBBED_FIELDS

    thread_id = _thread(db_session)
    ev = _event(db_session, thread_id, "all fields secret")
    trace = db_session.query(Event.trace_id).filter_by(id=ev).scalar()
    turn = _turn(db_session, thread_id)
    epoch_id, seg_id = _id(), _id()
    db_session.add(Epoch(id=epoch_id, thread_id=thread_id, epoch_no=1, status="open",
                         created_at=_now()))
    db_session.add(Segment(id=seg_id, epoch_id=epoch_id, thread_id=thread_id,
                           segment_no=1, status="open", start_turn_sequence=1,
                           end_turn_sequence=1, created_at=_now()))
    summary = SegmentSummary(
        id=_id(), segment_id=seg_id, goal="goal", outcome="outcome",
        decisions=["d"], source_event_ids=[ev], summary_version=1,
        token_count=1, created_at=_now(),
    )
    checkpoint = EpochCheckpoint(
        id=_id(), epoch_id=epoch_id, current_goal="goal",
        completed_milestones=["m"], source_segment_ids=[seg_id],
        source_hashes=["h"], version=1, token_count=1, created_at=_now(),
    )
    memory = MemoryRecord(
        id=_id(), memory_type="fact", canonical_key="k:all", scope_type="thread",
        scope_id=thread_id, content="memory", lifecycle_state="active",
        validity_state="valid", confidence=0.9, created_at=_now(),
        source_event_id=ev,
    )
    proposal = MemoryProposal(
        id=_id(), proposal_id="p-all", source_event_ids=[ev],
        canonical_key="k:all", content="proposal", structured_value={"v": 1},
        evidence=[{"span": "e"}], raw_payload={"r": 1}, normalized_payload={"n": 1},
        execution_mode="sync", retention_policy="standard", durable=True,
        created_at=_now(),
    )
    maint_in = MemoryMaintenanceInput(
        id=_id(), run_id=_id(), batch_id=_id(), input_sequence=1,
        memory_record_id=memory.id, input_role="primary", record_version=1,
        snapshot_json={"content": "snapshot"}, created_at=_now(),
    )
    maint_act = MemoryMaintenanceAction(
        id=_id(), run_id=_id(), batch_id=_id(), action_sequence=1,
        subject_memory_record_id=memory.id, related_record_ids=[memory.id],
        action_type="merge", status="done", details={"content": "d"},
        preconditions={"p": 1}, created_at=_now(),
    )
    block = CoreMemoryBlock(
        id=_id(), block_name=f"block-{_id()}", content="block content",
        source_memory_ids=[memory.id], projection_version=1, record_versions={},
        token_count=1, checksum="h", created_at=_now(), updated_at=_now(),
    )
    entry = RetrievalIndexEntry(
        id=_id(), source_type="memory_record", source_id=memory.id,
        source_version="1", index_version=1, thread_id=thread_id,
        memory_record_id=memory.id, scope_type="thread", scope_id=thread_id,
        canonical_key="k:all", lifecycle_state="active", validity_state="valid",
        retrieval_tier="warm", title="title", search_text="text", snippet="snip",
        metadata={"m": 1}, created_source_at=_now(), indexed_at=_now(),
        is_current=True, is_searchable=True,
    )
    compaction = CompactionInput(
        id=_id(), segment_id=seg_id, start_turn_sequence=1, end_turn_sequence=1,
        turn_manifest=[{"t": 1}], event_manifest=[{"e": 1}],
        working_state_version=1, working_state_snapshot={"x": 1},
        source_hash="h", summary_version=1, created_at=_now(),
    )
    epoch_compaction = EpochCompactionInput(
        id=_id(), epoch_id=epoch_id, boundary_turn_sequence=1,
        working_state_version=1, current_objective="obj",
        open_loops=["o"], active_constraints=["c"], artifact_refs=["a"],
        verified_tool_states=["v"], source_segment_ids=[seg_id],
        source_hashes=["h"], snapshot_hash="h", checkpoint_version=1,
        created_at=_now(),
    )
    db_session.add_all([summary, checkpoint, memory, proposal, maint_in, maint_act,
                        block, entry, compaction, epoch_compaction])
    db_session.flush()
    token = RetrievalIndexToken(
        id=_id(), entry_id=entry.id, index_version=1, token="leak",
        token_kind="word", term_frequency=1,
    )
    db_session.add(token)
    db_session.add(WorkingState(
        id=_id(), thread_id=thread_id, current_objective="obj",
        open_loops=["o"], active_constraints=["c"], pending_approvals=["p"],
        artifact_refs=["a"], verified_tool_states=["v"],
        uncommitted_side_effects=["u"], running_tool_state=["r"],
        applied_idempotency_keys=["k"],
    ))
    db_session.add(ContextSnapshot(
        id=_id(), trace_id=_id(), thread_id=thread_id, stable_prefix_hash="h",
        context_items=[{"x": 1}], meta={"m": 1}, token_total=1, created_at=_now(),
    ))
    db_session.add(LLMCall(
        id=_id(), trace_id=trace, thread_id=thread_id, model="m", latency_ms=1.0,
        input_preview="in", output_preview="out", created_at=_now(),
    ))
    reminder = Task(
        id=_id(), task_type="reminder", status="pending", title="title",
        description="desc", condition="cond", thread_id=thread_id,
        source_event_id=ev, created_at=_now(),
    )
    db_session.add(reminder)
    artifact = Artifact(
        id=_id(), thread_id=thread_id, kind="text", ref="r", content="content",
        content_hash="h", token_count=1, turn_record_id=turn.id, created_at=_now(),
    )
    db_session.add(artifact)
    db_session.add(AttentionState(
        id=_id(), thread_id=thread_id, focus_topic="topic",
        recent_topics=["a"], decision="none", created_at=_now(),
    ))
    db_session.add(TurnEventInbox(
        id=_id(), thread_id=thread_id, dedupe_key="d", event_type="e",
        source="s", priority=1, payload={"text": "inbox"}, status="pending",
        turn_record_id=turn.id, turn_id=turn.turn_id, created_at=_now(),
    ))
    outbox = OutboxJob(
        id=_id(), operation_id=f"memory_extraction:{turn.id}:x",
        job_type="memory_extraction", status="completed",
        payload={"content": "outbox", "memory_id": memory.id},
        trace_id=trace, error_message="err", created_at=_now(),
    )
    db_session.add(outbox)
    db_session.flush()
    tool = ToolOperation(
        id=_id(), idempotency_key="tool-1", thread_id=thread_id,
        turn_record_id=turn.id, turn_id=turn.turn_id, tool_call_id="call-1",
        capability_id="cap", params={"content": "params"}, params_hash="h",
        descriptor_hash="h", status="committed", effect_mode="db_transactional",
        outbox_job_id=outbox.id, result_payload={"content": "result"},
        effect_receipt={"content": "receipt"}, error_message="err",
        trace_id=trace, created_at=_now(),
    )
    task_rt = AgentTask(
        id=_id(), thread_id=thread_id, task_type="research", title="task",
        goal="goal", status="running", executor_type="llm", priority=0,
        task_brief={"initial_input": "brief"}, task_state={"current_action": "a"},
        report={"r": 1}, last_error="err", created_at=_now(),
    )
    db_session.add(task_rt)
    db_session.flush()
    run = AgentRun(
        id=_id(), task_id=task_rt.id, run_index=0, trigger="user",
        status="completed", input_snapshot={"i": 1}, output_summary={"o": 1},
        error_message="err", created_at=_now(),
    )
    action = AgentAction(
        id=_id(), task_id=task_rt.id, run_id=run.id, sequence=1,
        capability_id="cap", arguments={"a": 1}, arguments_hash="h",
        descriptor_hash="h", idempotency_key="act-1", status="succeeded",
        risk_level="low", requires_approval=False, preconditions=[{"p": 1}],
        effects=[{"e": 1}], result_summary={"r": 1}, evidence_refs=[{"x": 1}],
        error_message="err", created_at=_now(),
    )
    task_event = AgentTaskEvent(
        id=_id(), task_id=task_rt.id, sequence=1, event_type="e",
        visibility="conversation", payload={"text": "event"}, created_at=_now(),
    )
    checkpoint_rt = AgentTaskCheckpoint(
        id=_id(), task_id=task_rt.id, version=1, event_sequence=1,
        state={"s": 1}, plan={"p": 1}, pending_action_refs=[{"a": 1}],
        evidence_refs=[{"e": 1}], reason="r", snapshot_hash="h", created_at=_now(),
    )
    evidence = TaskEvidence(
        id=_id(), task_id=task_rt.id, kind="file", summary="summary",
        inline_payload={"i": 1}, content_hash="h", mime_type="text/plain",
        content_size=1, created_at=_now(),
    )
    task_artifact = TaskArtifact(
        id=_id(), task_id=task_rt.id, name="name", artifact_type="file",
        summary="summary", object_bucket="", object_key="",
        content_hash="h", mime_type="text/plain", content_size=1, created_at=_now(),
    )
    approval = ApprovalRequest(
        id=_id(), thread_id=thread_id, task_id=task_rt.id, action_id=action.id,
        tool_name="tool", tool_args={"content": "args"}, tool_args_hash="h",
        descriptor_hash="h", risk_snapshot={"r": 1}, preconditions={"p": 1},
        effects={"e": 1}, approval_hash="h", status="pending",
        execution_result={"content": "result"}, error_message="err",
        created_at=_now(),
    )
    watch = AgentTaskWatch(
        id=_id(), task_id=task_rt.id, watch_type="condition",
        config={"query": "watch"}, cursor={"cursor": 1}, status="active",
        created_at=_now(),
    )
    receipt = DesktopActionReceipt(
        action_id=action.id, node_id="node-1", idempotency_key="receipt-1",
        arguments_hash="h", status="committed",
        result_payload={"content": "receipt"}, error_message="err",
        updated_at=_now(),
    )
    db_session.add_all([tool, run, action, task_event, checkpoint_rt, evidence,
                        task_artifact, approval, watch, receipt])
    db_session.flush()
    case = MemoryConflictCase(
        id=_id(), status="pending", thread_id=thread_id, scope_type="thread",
        scope_id=thread_id, canonical_key="k:all", memory_type="fact",
        sensitivity="normal", old_memory_id=memory.id, old_record_version=1,
        old_content="old", new_content="new", proposal_id=proposal.id,
        proposal_payload={"content": "p"}, verdict="conflict",
        verdict_reason="reason", evidence=[{"e": 1}], graph_evidence={"g": 1},
        question="q", decision_source="user", decision_span="span",
        resolution_reason="res", idempotency_key="case-all", created_at=_now(),
    )
    recall = MemoryRecallRun(
        id=_id(), trace_id=trace, request_query="query",
        scope_context={"s": 1}, routes_executed=[{"r": 1}], created_at=_now(),
    )
    retrieval_run = RetrievalRun(
        id=_id(), trace_id=trace, query="retrieval", strategy="hybrid",
        created_at=_now(),
    )
    db_session.add_all([case, recall, retrieval_run])
    doc = Document(
        id=_id(), source_path="external/all.txt", content_hash="h",
        mime_type="text/plain", title="doc", doc_type="text", status="indexed",
        parser_version="p", chunker_version="c", created_at=_now(),
    )
    db_session.add(doc)
    db_session.flush()
    chunk = Chunk(
        id=_id(), document_id=doc.id, content="chunk", line_start=0, line_end=1,
        chunk_index=0, token_estimate=1, heading_path="", section_id="",
        block_type="text", source_type="text", parser_version="p",
        chunker_version="c", embedding_input_hash="h", created_at=_now(),
    )
    db_session.add(chunk)
    db_session.flush()
    db_session.add(KnowledgeChunkVector(
        chunk_id=chunk.id, content_hash="h", embedding_model="m",
        input_version="1", embedding=[0.0] * 512, created_at=_now(),
    ))
    op_id = _op(
        db_session, mode="everywhere", selector_type="time_range",
        payload={"time_from": _at(-120).isoformat(), "time_to": _at(120).isoformat()},
    )
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()
    scope = load_scope(db_session, op_id, "everywhere")
    assert scope is not None
    from aiive.forget.content_scrub import unknown_reference_details
    report = verify_scope(db_session, scope)
    assert report.ok, unknown_reference_details(db_session, scope)

    rows = {
        "events": ev, "turn_records": turn.id, "turn_event_inbox": db_session.query(TurnEventInbox).first().id,
        "memory_records": memory.id, "memory_proposals": proposal.id,
        "memory_maintenance_inputs": maint_in.id, "memory_maintenance_actions": maint_act.id,
        "retrieval_index_entries": entry.id,
        "segment_summaries": summary.id, "epoch_checkpoints": checkpoint.id,
        "working_states": db_session.query(WorkingState).first().id,
        "context_snapshots": db_session.query(ContextSnapshot).first().id,
        "llm_calls": db_session.query(LLMCall).first().id,
        "tasks": reminder.id, "artifacts": artifact.id,
        "attention_states": db_session.query(AttentionState).first().id,
        "tool_operations": tool.id, "approval_requests": approval.id,
        "agent_tasks": task_rt.id, "agent_runs": run.id, "agent_actions": action.id,
        "agent_task_events": task_event.id, "agent_task_checkpoints": checkpoint_rt.id,
        "task_evidence": evidence.id, "task_artifacts": task_artifact.id,
        "documents": doc.id, "memory_conflict_cases": case.id,
        "memory_recall_runs": recall.id, "retrieval_runs": retrieval_run.id,
        "outbox_jobs": outbox.id,
        "compaction_inputs": compaction.id,
        "epoch_compaction_inputs": epoch_compaction.id,
        "threads": thread_id,
        "agent_task_watches": watch.id,
        "desktop_action_receipts": action.id,
    }

    special = {
        "memory_evidence", "core_memory_blocks", "retrieval_index_tokens",
        "chunks", "knowledge_chunk_vectors",
    }
    json_columns = {
        "structured_value", "graph_facts", "evidence", "raw_payload",
        "normalized_payload", "proposal_payload", "graph_evidence",
        "snapshot_json", "details", "preconditions", "effects", "risk_snapshot",
        "execution_result", "tool_args", "params", "result_payload",
        "effect_receipt", "input_snapshot", "output_summary", "arguments",
        "result_summary", "evidence_refs", "pending_action_refs", "inline_payload",
        "payload", "task_brief", "task_state", "state", "plan", "context_items",
        "meta", "scope_context", "routes_executed", "metadata",
        "working_state_snapshot", "turn_manifest", "event_manifest",
        "open_loops", "active_constraints", "artifact_refs",
        "verified_tool_states", "pending_approvals", "uncommitted_side_effects",
        "running_tool_state", "applied_idempotency_keys", "recent_topics",
    }

    def _inject(label: str) -> None:
        table_name, column = label.split(".")
        if table_name in special:
            return
        table = Base.metadata.tables[table_name]
        column_obj = table.c[column]
        row_id = rows[table_name]
        pk = "action_id" if table_name == "desktop_action_receipts" else "id"
        stmt = select(column_obj).where(table.c[pk] == row_id)
        original = db_session.execute(stmt).scalar()
        if column == "is_searchable" or column == "is_current":
            value: Any = True
        elif column == "status":
            value = "indexed"
        elif column in json_columns:
            value = {"leak": 1}
        else:
            value = "leak"
        db_session.execute(
            update(table).where(table.c[pk] == row_id).values({column: value})
        )
        db_session.commit()
        report = verify_scope(db_session, scope)
        assert not report.ok, f"residue not detected: {label}"
        db_session.execute(
            update(table).where(table.c[pk] == row_id).values({column: original})
        )
        db_session.commit()

    for label in sorted(SCRUBBED_FIELDS):
        _inject(label)

    # 被物理删除的行：重新插入残留后 Verify 必须发现。
    db_session.add(MemoryEvidence(
        id=_id(), memory_id=memory.id, source_event_id=ev,
        source_type="user_assertion", trust_level="trusted", relation="support",
        content_span="leak", created_at=_now(),
    ))
    db_session.commit()
    assert not verify_scope(db_session, scope).ok
    db_session.query(MemoryEvidence).delete()
    db_session.commit()

    db_session.add(CoreMemoryBlock(
        id=_id(), block_name=f"leak-{_id()}", content="leak",
        source_memory_ids=[memory.id], projection_version=1, record_versions={},
        token_count=1, checksum="h", created_at=_now(), updated_at=_now(),
    ))
    db_session.commit()
    assert not verify_scope(db_session, scope).ok
    db_session.query(CoreMemoryBlock).filter(CoreMemoryBlock.content == "leak").delete()
    db_session.commit()

    db_session.add(RetrievalIndexToken(
        id=_id(), entry_id=entry.id, index_version=1, token="leak2",
        token_kind="word", term_frequency=1,
    ))
    db_session.commit()
    assert not verify_scope(db_session, scope).ok
    db_session.query(RetrievalIndexToken).delete()
    db_session.commit()

    db_session.add(Chunk(
        id=_id(), document_id=doc.id, content="leak", line_start=0, line_end=1,
        chunk_index=0, token_estimate=1, heading_path="", section_id="",
        block_type="text", source_type="text", parser_version="p",
        chunker_version="c", embedding_input_hash="h", created_at=_now(),
    ))
    db_session.commit()
    assert not verify_scope(db_session, scope).ok
    db_session.query(Chunk).delete()
    db_session.commit()

    chunk_action = db_session.query(ForgetAction).filter_by(
        idempotency_key=f"fo:{op_id}:knowledge_chunks",
    ).first()
    stored = (chunk_action.details or {}).get("chunk_ids") if chunk_action else []
    assert stored
    db_session.add(KnowledgeChunkVector(
        chunk_id=stored[0], content_hash="h", embedding_model="m",
        input_version="1", embedding=[0.0] * 512, created_at=_now(),
    ))
    db_session.commit()
    assert not verify_scope(db_session, scope).ok
    db_session.query(KnowledgeChunkVector).delete()
    db_session.commit()


def test_outbox_without_trace_is_linked_by_operation_ids(db_session: Session) -> None:
    thread_id = _thread(db_session)
    turn = _turn(db_session, thread_id)
    outbox = OutboxJob(
        id=_id(), operation_id=f"memory_extraction:{turn.id}:abc",
        job_type="memory_extraction", status="completed",
        payload={"content": "no-trace content"}, trace_id=None,
        error_message="no-trace error", created_at=_now(),
    )
    db_session.add(outbox)
    op_id = _op(db_session, mode="everywhere", selector_type="thread",
                payload={"thread_id": thread_id})
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()

    outbox = db_session.query(OutboxJob).filter_by(id=outbox.id).first()
    assert outbox.payload["content"] == "[redacted]"
    assert outbox.error_message == ""


def test_scrub_fields_subset_of_verify_fields() -> None:
    from aiive.forget.content_scrub import SCRUBBED_FIELDS, VERIFIED_FIELDS

    assert SCRUBBED_FIELDS <= VERIFIED_FIELDS
    assert "agent_runs.error_message" in VERIFIED_FIELDS
    assert "tool_operations.effect_receipt" in VERIFIED_FIELDS
    assert "memory_conflict_cases.old_content" in VERIFIED_FIELDS


def test_unified_retriever_cannot_recall_after_forget(db_session: Session) -> None:
    from aiive.retrieval.indexing_service import refresh_source
    from aiive.retrieval.retrieval_index import RetrievalIndexManager
    from aiive.retrieval.retrieval_types import RetrievalMode, RetrievalRequest
    from aiive.retrieval.unified_retriever import UnifiedRetriever

    gen = RetrievalIndexManager().create_generation(db_session, 1, status="active")
    assert gen.index_version == 1
    thread_id = _thread(db_session)
    ev = _event(db_session, thread_id, "Python 编程偏好")
    seg = Segment(
        id=_id(), epoch_id=_id(), thread_id=thread_id, segment_no=1, status="open",
        start_turn_sequence=1, end_turn_sequence=1, created_at=_now(),
    )
    db_session.add(seg)
    db_session.add(Epoch(
        id=seg.epoch_id, thread_id=thread_id, epoch_no=1, status="open",
        created_at=_now(),
    ))
    summary = SegmentSummary(
        id=_id(), segment_id=seg.id, goal="Python 编程偏好",
        outcome="用户明确选择 Python", source_event_ids=[ev],
        summary_version=1, token_count=1, source_hash="h", created_at=_now(),
    )
    memory = MemoryRecord(
        id=_id(), memory_type="fact", canonical_key="user.pref.lang",
        scope_type="thread", scope_id=thread_id, content="用户偏好 Python",
        lifecycle_state="active", validity_state="valid", confidence=0.9,
        created_at=_now(),
    )
    db_session.add_all([summary, memory])
    db_session.flush()
    refresh_source(db_session, "segment_summary", summary.id, "memory.created")
    refresh_source(db_session, "memory_record", memory.id, "memory.created")
    db_session.commit()

    ur = UnifiedRetriever(db_session)
    exact = ur.retrieve(RetrievalRequest(
        query="user.pref.lang", mode=RetrievalMode.SEARCH,
        source_types=["memory_record"], scope_context={},
    ))
    lexical = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.SEARCH,
        source_types=["segment_summary"],
    ))
    assert any(h.source_id == memory.id for h in exact.hits)
    assert any(h.source_id == summary.id for h in lexical.hits)

    from aiive.db.forget_models import ForgetTarget
    op_id = _op(db_session, mode="everywhere", selector_type="thread",
                payload={"thread_id": thread_id})
    db_session.add(ForgetTarget(
        id=_id(), forget_operation_id=op_id, target_type="memory_record",
        target_id=memory.id, batch_no=0, frozen_at=_now(),
    ))
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()

    exact_after = ur.retrieve(RetrievalRequest(
        query="user.pref.lang", mode=RetrievalMode.SEARCH,
        source_types=["memory_record"], scope_context={},
    ))
    lexical_after = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.SEARCH,
        source_types=["segment_summary"],
    ))
    assert exact_after.hits == []
    assert lexical_after.hits == []
    assert db_session.query(RetrievalIndexToken).count() == 0
    entries = db_session.query(RetrievalIndexEntry).all()
    assert entries and all(not e.is_current and not e.is_searchable for e in entries)


def _run_job(handler, op_id: str, db_session: Session, monkeypatch):
    from aiive.worker import handlers_forget
    from aiive.worker.outbox_dto import ClaimedJob

    engine = db_session.get_bind()
    monkeypatch.setattr(handlers_forget, "SessionLocal", sessionmaker(bind=engine))
    claimed = ClaimedJob(
        id=_id(), job_type="forget", payload={"forget_operation_id": op_id},
        trace_id="t", retry_count=0, max_retries=3, claim_token="ct",
        schema_version=1, worker_id="w",
    )
    return handler(claimed)


def test_purge_verify_chain_and_residue_keeps_shield(
    db_session: Session, tmp_path: Path, monkeypatch,
) -> None:
    from aiive.worker.handlers_forget import (
        handle_forget_purge,
        handle_forget_verify,
    )

    thread_id = _thread(db_session)
    _event(db_session, thread_id)
    db_session.add(WorkingState(id=_id(), thread_id=thread_id, current_objective="obj"))
    bucket, key = _object(tmp_path, b"purge-chain-object")
    task = AgentTask(
        id=_id(), thread_id=thread_id, task_type="research", title="t", goal="g",
        status="queued", executor_type="llm", priority=0, task_brief={},
        task_state={}, created_at=_now(),
    )
    db_session.add(task)
    db_session.add(TaskEvidence(
        id=_id(), task_id=task.id, kind="file", summary="s",
        object_bucket=bucket, object_key=key, content_hash="h",
        mime_type="text/plain", content_size=1, created_at=_now(),
    ))
    op_id = _op(db_session, mode="everywhere", selector_type="thread",
                payload={"thread_id": thread_id})
    db_session.add(ForgetShield(
        id=_id(), forget_operation_id=op_id, selector_type="thread",
        thread_id=thread_id, cutoff_created_at=_now(), status="active",
        normalized_shield_key=f"shield:{op_id}", created_at=_now(),
    ))
    db_session.commit()

    purge = _run_job(handle_forget_purge, op_id, db_session, monkeypatch)
    assert purge.outcome.value == "completed", purge
    assert not object_store.exists(object_store.ObjectRef(bucket, key))
    db_session.expire_all()
    assert db_session.query(WorkingState).first().current_objective is None

    verify = _run_job(handle_forget_verify, op_id, db_session, monkeypatch)
    assert verify.outcome.value == "completed"
    db_session.expire_all()
    op = db_session.query(ForgetOperation).filter_by(id=op_id).first()
    assert op.status == "verified_with_coarse_purge"
    assert db_session.query(ForgetShield).filter_by(forget_operation_id=op_id).first().status == "retired"

    # 人为制造残留 → 重新 Verify 必须 failed_retryable，Shield 保持 active。
    db_session.execute(
        sa_text("UPDATE events SET payload = '{\"text\": \"leak\"}' WHERE thread_id = :tid"),
        {"tid": thread_id},
    )
    db_session.add(ForgetShield(
        id=_id(), forget_operation_id=op_id, selector_type="thread",
        thread_id=thread_id, cutoff_created_at=_now(), status="active",
        normalized_shield_key=f"shield2:{op_id}", created_at=_now(),
    ))
    db_session.commit()

    verify2 = _run_job(handle_forget_verify, op_id, db_session, monkeypatch)
    assert verify2.outcome.value == "completed"
    db_session.expire_all()
    op = db_session.query(ForgetOperation).filter_by(id=op_id).first()
    assert op.status == "failed_retryable"
    assert db_session.query(ForgetShield).filter(
        ForgetShield.forget_operation_id == op_id, ForgetShield.status == "active",
    ).count() >= 1


def test_json_array_membership_handles_null_empty_and_duplicates(
    db_session: Session,
) -> None:
    from aiive.forget.content_scope import count_json_array_members, select_json_array_members

    def summary(sid: str, events) -> None:
        db_session.add(SegmentSummary(
            id=_id(), segment_id=sid, source_event_ids=events,
            summary_version=1, token_count=0, created_at=_now(),
        ))

    summary("s-null", None)
    summary("s-empty", [])
    summary("s-one", ["e1", "e1"])
    summary("s-nested", [{"id": "e2"}])
    db_session.commit()

    assert select_json_array_members(
        db_session, "segment_summaries", "id", "source_event_ids", ["e1"],
    )
    assert count_json_array_members(
        db_session, "segment_summaries", "source_event_ids", ["e1"],
    ) == 1
    assert count_json_array_members(
        db_session, "segment_summaries", "source_event_ids", ["nope"],
    ) == 0
    assert count_json_array_members(
        db_session, "segment_summaries", "source_event_ids", [],
    ) == 0
