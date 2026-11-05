"""PostgreSQL 专项：Forget 范围冻结、Task 子记录、辅助表、检索一致性与未知结构。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from aiive.db.forget_models import (
    ForgetOperation,
    ForgetSelectorManifest,
    ForgetTarget,
)
from aiive.db.models import (
    AgentRun,
    AgentTask,
    AgentTaskCheckpoint,
    Epoch,
    Event,
    MemoryConflictCase,
    MemoryRecallRun,
    MemoryRecord,
    OutboxJob,
    RetrievalIndexEntry,
    RetrievalIndexToken,
    RetrievalRun,
    Segment,
    SegmentSummary,
    TaskArtifact,
    TaskEvidence,
    Thread,
    TurnRecord,
)
from aiive.forget.content_scrub import process_object_deletions, verify_scope
from aiive.forget.content_scope import load_scope
from aiive.retrieval.indexing_service import refresh_source
from aiive.retrieval.retrieval_index import RetrievalIndexManager
from aiive.retrieval.retrieval_types import RetrievalMode, RetrievalRequest
from aiive.retrieval.unified_retriever import UnifiedRetriever
from aiive.worker.handlers_forget import _scrub_wide

pytestmark = pytest.mark.pg


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _at(seconds: int) -> datetime:
    return _now() + timedelta(seconds=seconds)


def _thread(s: Session) -> str:
    tid = _id()
    s.add(Thread(id=tid))
    s.flush()
    return tid


def _event(s: Session, thread_id: str, text: str, created_at: datetime) -> str:
    eid = _id()
    s.add(Event(
        id=eid, trace_id=_id(), thread_id=thread_id, event_type="user_message",
        payload={"text": text}, created_at=created_at,
    ))
    s.flush()
    return eid


def _op(s: Session, *, mode: str, selector: str, payload: dict) -> str:
    op_id = _id()
    s.add(ForgetOperation(
        id=op_id, operation_key=f"forget:{op_id}", mode=mode,
        selector_type=selector, selector_hash="h", status="shielded",
        requested_by="test", target_count=0, shielded_at=_now(),
    ))
    s.add(ForgetSelectorManifest(
        id=_id(), forget_operation_id=op_id, selector_type=selector,
        selector_payload={"selector_type": selector, **payload},
        selector_payload_hash="h", cutoff_created_at=_now(),
    ))
    s.flush()
    return op_id


def test_pg_cutoff_retention_and_derived_cleanup(pg_sessionmaker) -> None:
    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        old_ev = _event(s, thread_id, "old", _at(-60))
        new_ev = _event(s, thread_id, "new", _at(60))
        old_memory = MemoryRecord(
            id=_id(), memory_type="fact", canonical_key="k:old",
            scope_type="thread", scope_id=thread_id, content="old memory",
            lifecycle_state="active", validity_state="valid", confidence=0.9,
            created_at=_at(-60), source_event_id=old_ev,
        )
        new_memory = MemoryRecord(
            id=_id(), memory_type="fact", canonical_key="k:new",
            scope_type="thread", scope_id=thread_id, content="new memory",
            lifecycle_state="active", validity_state="valid", confidence=0.9,
            created_at=_at(60), source_event_id=new_ev,
        )
        s.add_all([old_memory, new_memory])
        s.flush()
        epoch_id, seg_id = _id(), _id()
        s.add(Epoch(id=epoch_id, thread_id=thread_id, epoch_no=1, status="open",
                    created_at=_now()))
        s.flush()
        s.add(Segment(id=seg_id, epoch_id=epoch_id, thread_id=thread_id,
                      segment_no=1, status="open", start_turn_sequence=1,
                      end_turn_sequence=1, created_at=_now()))
        s.flush()
        s.add(SegmentSummary(
            id=_id(), segment_id=seg_id, goal="old-derived", outcome="o",
            source_event_ids=[old_ev], summary_version=1, token_count=1,
            created_at=_at(60),
        ))
        op_id = _op(s, mode="everywhere", selector="thread",
                    payload={"thread_id": thread_id})
        s.add(ForgetTarget(
            id=_id(), forget_operation_id=op_id, target_type="memory_record",
            target_id=old_memory.id, batch_no=0, frozen_at=_now(),
        ))
        s.commit()

        _scrub_wide(s, op_id)
        s.commit()
        s.expire_all()

        assert s.query(Event).filter_by(id=old_ev).first().payload == {}
        assert s.query(Event).filter_by(id=new_ev).first().payload == {"text": "new"}
        assert s.query(MemoryRecord).filter_by(id=old_memory.id).first().content == "[forgotten]"
        assert s.query(MemoryRecord).filter_by(id=new_memory.id).first().content == "new memory"
        assert s.query(SegmentSummary).first().goal == "[redacted]"
        scope = load_scope(s, op_id, "everywhere")
        assert scope is not None and verify_scope(s, scope).ok
    finally:
        s.close()


def test_pg_task_time_range_children(pg_sessionmaker) -> None:
    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        task = AgentTask(
            id=_id(), thread_id=thread_id, task_type="research", title="long task",
            goal="goal", status="running", executor_type="llm", priority=0,
            task_brief={"initial_input": "keep"}, task_state={},
            created_at=_at(-3600),
        )
        s.add(task)
        s.flush()
        run_old = AgentRun(
            id=_id(), task_id=task.id, run_index=0, trigger="user",
            status="completed", input_snapshot={"old": 1}, created_at=_at(-3600),
        )
        run_new = AgentRun(
            id=_id(), task_id=task.id, run_index=1, trigger="user",
            status="running", input_snapshot={"new": 1}, created_at=_at(-60),
        )
        ev_old = TaskEvidence(
            id=_id(), task_id=task.id, kind="file", summary="old",
            content_hash="h", mime_type="text/plain", content_size=1,
            created_at=_at(-3600),
        )
        ev_new = TaskEvidence(
            id=_id(), task_id=task.id, kind="file", summary="new",
            inline_payload={"new": 1}, content_hash="h", mime_type="text/plain",
            content_size=1, created_at=_at(-60),
        )
        art_old = TaskArtifact(
            id=_id(), task_id=task.id, name="old", artifact_type="file",
            summary="old", object_bucket="", object_key="", content_hash="h",
            mime_type="text/plain", content_size=1, created_at=_at(-3600),
        )
        art_new = TaskArtifact(
            id=_id(), task_id=task.id, name="new", artifact_type="file",
            summary="new", object_bucket="", object_key="", content_hash="h",
            mime_type="text/plain", content_size=1, created_at=_at(-60),
        )
        checkpoint_old = AgentTaskCheckpoint(
            id=_id(), task_id=task.id, version=1, event_sequence=1,
            state={"old": 1}, plan={}, reason="r", snapshot_hash="h",
            created_at=_at(-3600),
        )
        checkpoint_new = AgentTaskCheckpoint(
            id=_id(), task_id=task.id, version=2, event_sequence=2,
            state={"new": 1}, plan={}, reason="r", snapshot_hash="h",
            created_at=_at(-60),
        )
        s.add_all([run_old, run_new, ev_old, ev_new, art_old, art_new,
                   checkpoint_old, checkpoint_new])
        task.task_state = {"evidence_id": ev_new.id}
        op_id = _op(
            s, mode="everywhere", selector="time_range",
            payload={"time_from": _at(-120).isoformat(), "time_to": _at(0).isoformat()},
        )
        s.commit()

        _scrub_wide(s, op_id)
        s.commit()
        s.expire_all()

        task = s.query(AgentTask).filter_by(id=task.id).first()
        assert task.title == "long task" and task.status == "cancelled"
        assert task.task_state["evidence_id"] == "[forgotten]"
        assert s.query(AgentRun).filter_by(id=run_old.id).first().input_snapshot == {"old": 1}
        assert s.query(AgentRun).filter_by(id=run_new.id).first().input_snapshot == {}
        assert s.query(TaskEvidence).filter_by(id=ev_old.id).first().summary == "old"
        assert s.query(TaskEvidence).filter_by(id=ev_new.id).first().summary == "[redacted]"
        assert s.query(TaskArtifact).filter_by(id=art_old.id).first().name == "old"
        assert s.query(TaskArtifact).filter_by(id=art_new.id).first().name == "[redacted]"
        assert s.query(AgentTaskCheckpoint).filter_by(id=checkpoint_old.id).first().state == {"old": 1}
        assert s.query(AgentTaskCheckpoint).filter_by(id=checkpoint_new.id).first().state == {}
        process_object_deletions(s, op_id)
        scope = load_scope(s, op_id, "everywhere")
        assert scope is not None and verify_scope(s, scope).ok
    finally:
        s.close()


def test_pg_conflict_and_auxiliary_tables(pg_sessionmaker) -> None:
    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        ev = _event(s, thread_id, "aux", _now())
        trace = s.query(Event.trace_id).filter_by(id=ev).scalar()
        memory = MemoryRecord(
            id=_id(), memory_type="fact", canonical_key="k:aux",
            scope_type="thread", scope_id=thread_id, content="memory",
            lifecycle_state="active", validity_state="valid", confidence=0.9,
            created_at=_now(),
        )
        s.add(memory)
        s.flush()
        case = MemoryConflictCase(
            id=_id(), status="pending", thread_id=thread_id, scope_type="thread",
            scope_id=thread_id, canonical_key="k:aux", memory_type="fact",
            sensitivity="normal", old_memory_id=memory.id, old_record_version=1,
            old_content="old", new_content="new", proposal_id="p1",
            proposal_payload={"content": "p"}, verdict="conflict",
            verdict_reason="reason", evidence=[{"e": 1}], graph_evidence={"g": 1},
            question="q", decision_source="user", decision_span="span",
            resolution_reason="res", idempotency_key="case-1", created_at=_now(),
        )
        recall = MemoryRecallRun(
            id=_id(), trace_id=trace, request_query="query",
            scope_context={"s": 1}, routes_executed=[{"r": 1}], created_at=_now(),
        )
        retrieval_run = RetrievalRun(
            id=_id(), trace_id=trace, query="retrieval", strategy="hybrid",
            created_at=_now(),
        )
        outbox = OutboxJob(
            id=_id(), operation_id=f"memory_extraction:{memory.id}:x",
            job_type="memory_extraction", status="completed",
            payload={"content": "outbox"}, trace_id=None, error_message="err",
            created_at=_now(),
        )
        s.add_all([case, recall, retrieval_run, outbox])
        op_id = _op(s, mode="everywhere", selector="thread",
                    payload={"thread_id": thread_id})
        s.add(ForgetTarget(
            id=_id(), forget_operation_id=op_id, target_type="memory_record",
            target_id=memory.id, batch_no=0, frozen_at=_now(),
        ))
        s.commit()

        _scrub_wide(s, op_id)
        s.commit()
        s.expire_all()

        case = s.query(MemoryConflictCase).first()
        assert case.status == "stale" and case.old_content == "[redacted]"
        assert case.proposal_payload == {} and case.evidence == []
        assert s.query(MemoryRecallRun).first().request_query == "[redacted]"
        assert s.query(RetrievalRun).first().query == "[redacted]"
        outbox = s.query(OutboxJob).first()
        assert outbox.payload["content"] == "[redacted]"
        assert outbox.error_message == ""
        scope = load_scope(s, op_id, "everywhere")
        assert scope is not None and verify_scope(s, scope).ok
    finally:
        s.close()


def test_pg_unknown_json_structure_scoped(pg_sessionmaker) -> None:
    s: Session = pg_sessionmaker()
    try:
        target_thread = _thread(s)
        other_thread = _thread(s)
        target_ev = _event(s, target_thread, "t", _now())
        other_ev = _event(s, other_thread, "o", _now())
        for thread_id, ev, obj in (
            (target_thread, target_ev, {"id": target_ev}),
            (other_thread, other_ev, {"id": other_ev}),
        ):
            epoch_id, seg_id = _id(), _id()
            s.add(Epoch(id=epoch_id, thread_id=thread_id, epoch_no=1,
                        status="open", created_at=_now()))
            s.flush()
            s.add(Segment(id=seg_id, epoch_id=epoch_id, thread_id=thread_id,
                          segment_no=1, status="open", start_turn_sequence=1,
                          end_turn_sequence=1, created_at=_now()))
            s.flush()
            s.add(SegmentSummary(
                id=_id(), segment_id=seg_id, goal="goal",
                source_event_ids=[obj], summary_version=1, token_count=1,
                created_at=_now(),
            ))
        op_id = _op(s, mode="everywhere", selector="thread",
                    payload={"thread_id": target_thread})
        s.commit()

        _scrub_wide(s, op_id)
        s.commit()
        s.expire_all()
        scope = load_scope(s, op_id, "everywhere")
        assert scope is not None
        report = verify_scope(s, scope)
        assert report.unknown == 1 and not report.ok
    finally:
        s.close()


def test_pg_field_residue_detection(pg_sessionmaker) -> None:
    from sqlalchemy import select, update

    from aiive.db.models import (
        AgentAction,
        ApprovalRequest,
        Base,
        CompactionInput,
        DesktopActionReceipt,
        DesktopNode,
        ToolOperation,
    )

    s: Session = pg_sessionmaker()
    try:
        thread_id = _thread(s)
        turn = TurnRecord(
            id=_id(), thread_id=thread_id, turn_id=_id(), turn_sequence=1,
            status="completed", request_fingerprint="f", source="chat",
            created_at=_now(),
        )
        s.add(turn)
        s.flush()
        ev = _event(s, thread_id, "residue", _now())
        trace = s.query(Event.trace_id).filter_by(id=ev).scalar()
        epoch_id, seg_id = _id(), _id()
        s.add(Epoch(id=epoch_id, thread_id=thread_id, epoch_no=1,
                    status="open", created_at=_now()))
        s.flush()
        s.add(Segment(id=seg_id, epoch_id=epoch_id, thread_id=thread_id,
                      segment_no=1, status="open", start_turn_sequence=1,
                      end_turn_sequence=1, created_at=_now()))
        s.flush()
        compaction = CompactionInput(
            id=_id(), segment_id=seg_id, start_turn_sequence=1,
            end_turn_sequence=1, turn_manifest=[{"t": 1}],
            event_manifest=[{"e": 1}], working_state_version=1,
            working_state_snapshot={"x": 1}, source_hash="h",
            summary_version=1, created_at=_now(),
        )
        task = AgentTask(
            id=_id(), thread_id=thread_id, task_type="research", title="t",
            goal="g", status="running", executor_type="llm", priority=0,
            task_brief={}, task_state={}, created_at=_now(),
        )
        s.add_all([compaction, task])
        s.flush()
        run = AgentRun(
            id=_id(), task_id=task.id, run_index=0, trigger="user",
            status="running", input_snapshot={"i": 1}, error_message="e",
            created_at=_now(),
        )
        s.add(run)
        s.flush()
        action = AgentAction(
            id=_id(), task_id=task.id, run_id=run.id, sequence=1,
            capability_id="cap", arguments={"a": 1}, arguments_hash="h",
            descriptor_hash="h", idempotency_key="a1", status="succeeded",
            risk_level="low", requires_approval=False, preconditions=[{"p": 1}],
            effects=[{"e": 1}], result_summary={"r": 1}, evidence_refs=[{"x": 1}],
            error_message="e", created_at=_now(),
        )
        checkpoint = AgentTaskCheckpoint(
            id=_id(), task_id=task.id, version=1, event_sequence=1,
            state={"s": 1}, plan={"p": 1}, pending_action_refs=[{"p": 1}],
            evidence_refs=[{"e": 1}], reason="r", snapshot_hash="h",
            created_at=_now(),
        )
        evidence = TaskEvidence(
            id=_id(), task_id=task.id, kind="file", summary="s",
            inline_payload={"i": 1}, content_hash="h", mime_type="text/plain",
            content_size=1, created_at=_now(),
        )
        artifact = TaskArtifact(
            id=_id(), task_id=task.id, name="n", artifact_type="file",
            summary="s", object_bucket="", object_key="", content_hash="h",
            mime_type="text/plain", content_size=1, created_at=_now(),
        )
        s.add_all([action, checkpoint, evidence, artifact])
        s.flush()
        outbox = OutboxJob(
            id=_id(), operation_id=f"tool:{action.id}", job_type="tool_call",
            status="completed", payload={"content": "c"},
            trace_id=trace, created_at=_now(),
        )
        s.add(outbox)
        s.flush()
        tool = ToolOperation(
            id=_id(), idempotency_key="tool-1", thread_id=thread_id,
            turn_record_id=turn.id, turn_id=turn.turn_id, tool_call_id="call",
            capability_id="cap", params={"content": "p"}, params_hash="h",
            descriptor_hash="h", status="committed",
            effect_mode="db_transactional", outbox_job_id=outbox.id,
            result_payload={"content": "r"}, effect_receipt={"content": "e"},
            error_message="e", trace_id=trace, created_at=_now(),
        )
        approval = ApprovalRequest(
            id=_id(), thread_id=thread_id, task_id=task.id, action_id=action.id,
            tool_name="tool", tool_args={"content": "a"}, tool_args_hash="h",
            descriptor_hash="h", risk_snapshot={"r": 1},
            preconditions={"p": 1}, effects={"e": 1}, approval_hash="h",
            status="pending", execution_result={"content": "r"},
            error_message="e", created_at=_now(),
        )
        s.add(DesktopNode(
            id="node", name="node", platform="test", arch="x",
        ))
        s.flush()
        receipt = DesktopActionReceipt(
            action_id=action.id, node_id="node", idempotency_key="r1",
            arguments_hash="h", status="committed",
            result_payload={"content": "r"}, error_message="e",
            updated_at=_now(),
        )
        case = MemoryConflictCase(
            id=_id(), status="pending", thread_id=thread_id, scope_type="thread",
            scope_id=thread_id, canonical_key="k", memory_type="fact",
            sensitivity="normal", old_memory_id=None, old_record_version=1,
            old_content="o", new_content="n", proposal_id="p1",
            proposal_payload={"content": "p"}, verdict="conflict",
            verdict_reason="r", evidence=[{"e": 1}], graph_evidence={"g": 1},
            question="q", decision_source="user", decision_span="d",
            resolution_reason="r", idempotency_key="c1", created_at=_now(),
        )
        recall = MemoryRecallRun(
            id=_id(), trace_id=trace, request_query="q",
            scope_context={"s": 1}, routes_executed=[{"r": 1}], created_at=_now(),
        )
        retrieval_run = RetrievalRun(
            id=_id(), trace_id=trace, query="q", strategy="hybrid",
            created_at=_now(),
        )
        s.add_all([tool, approval, receipt, case, recall, retrieval_run])
        op_id = _op(s, mode="everywhere", selector="thread",
                    payload={"thread_id": thread_id})
        s.commit()

        _scrub_wide(s, op_id)
        s.commit()
        scope = load_scope(s, op_id, "everywhere")
        assert scope is not None and verify_scope(s, scope).ok

        checks = [
            ("agent_runs", "error_message", run.id, "leak"),
            ("agent_actions", "effects", action.id, {"leak": 1}),
            ("agent_actions", "evidence_refs", action.id, [{"leak": 1}]),
            ("agent_actions", "error_message", action.id, "leak"),
            ("agent_task_checkpoints", "pending_action_refs", checkpoint.id, [{"leak": 1}]),
            ("agent_task_checkpoints", "evidence_refs", checkpoint.id, [{"leak": 1}]),
            ("task_evidence", "inline_payload", evidence.id, {"leak": 1}),
            ("task_artifacts", "name", artifact.id, "leak"),
            ("approval_requests", "risk_snapshot", approval.id, {"leak": 1}),
            ("approval_requests", "error_message", approval.id, "leak"),
            ("tool_operations", "effect_receipt", tool.id, {"leak": 1}),
            ("tool_operations", "error_message", tool.id, "leak"),
            ("compaction_inputs", "turn_manifest", compaction.id, [{"leak": 1}]),
            ("memory_conflict_cases", "old_content", case.id, "leak"),
            ("memory_recall_runs", "request_query", recall.id, "leak"),
            ("retrieval_runs", "query", retrieval_run.id, "leak"),
            ("outbox_jobs", "payload", outbox.id, {"content": "leak"}),
        ]
        for table_name, column, row_id, value in checks:
            table = Base.metadata.tables[table_name]
            stmt = select(table.c[column]).where(table.c.id == row_id)
            original = s.execute(stmt).scalar()
            s.execute(
                update(table).where(table.c.id == row_id).values({column: value})
            )
            s.commit()
            assert not verify_scope(s, scope).ok, f"{table_name}.{column}"
            s.execute(
                update(table).where(table.c.id == row_id).values({column: original})
            )
            s.commit()
    finally:
        s.close()


def test_pg_unified_retriever_recall_after_forget(pg_sessionmaker) -> None:
    s: Session = pg_sessionmaker()
    try:
        RetrievalIndexManager().create_generation(s, 1, status="active")
        thread_id = _thread(s)
        ev = _event(s, thread_id, "Python 编程偏好", _now())
        epoch_id, seg_id = _id(), _id()
        s.add(Epoch(id=epoch_id, thread_id=thread_id, epoch_no=1,
                    status="open", created_at=_now()))
        s.flush()
        s.add(Segment(id=seg_id, epoch_id=epoch_id, thread_id=thread_id,
                      segment_no=1, status="open", start_turn_sequence=1,
                      end_turn_sequence=1, created_at=_now()))
        s.flush()
        summary = SegmentSummary(
            id=_id(), segment_id=seg_id, goal="Python 编程偏好",
            outcome="选择 Python", source_event_ids=[ev], summary_version=1,
            token_count=1, source_hash="h", created_at=_now(),
        )
        memory = MemoryRecord(
            id=_id(), memory_type="fact", canonical_key="user.pref.lang",
            scope_type="thread", scope_id=thread_id, content="用户偏好 Python",
            lifecycle_state="active", validity_state="valid", confidence=0.9,
            created_at=_now(),
        )
        s.add_all([summary, memory])
        s.flush()
        refresh_source(s, "segment_summary", summary.id, "memory.created")
        refresh_source(s, "memory_record", memory.id, "memory.created")
        s.commit()

        ur = UnifiedRetriever(s)
        before_exact = ur.retrieve(RetrievalRequest(
            query="user.pref.lang", mode=RetrievalMode.SEARCH,
            source_types=["memory_record"], scope_context={},
        ))
        before_lexical = ur.retrieve(RetrievalRequest(
            query="Python", mode=RetrievalMode.SEARCH,
            source_types=["segment_summary"],
        ))
        assert any(h.source_id == memory.id for h in before_exact.hits)
        assert any(h.source_id == summary.id for h in before_lexical.hits)

        op_id = _op(s, mode="everywhere", selector="thread",
                    payload={"thread_id": thread_id})
        s.add(ForgetTarget(
            id=_id(), forget_operation_id=op_id, target_type="memory_record",
            target_id=memory.id, batch_no=0, frozen_at=_now(),
        ))
        s.commit()
        _scrub_wide(s, op_id)
        s.commit()
        s.expire_all()

        after_exact = ur.retrieve(RetrievalRequest(
            query="user.pref.lang", mode=RetrievalMode.SEARCH,
            source_types=["memory_record"], scope_context={},
        ))
        after_lexical = ur.retrieve(RetrievalRequest(
            query="Python", mode=RetrievalMode.SEARCH,
            source_types=["segment_summary"],
        ))
        assert after_exact.hits == [] and after_lexical.hits == []
        assert s.query(RetrievalIndexToken).count() == 0
        assert all(
            not e.is_searchable and not e.is_current
            for e in s.query(RetrievalIndexEntry).all()
        )
    finally:
        s.close()
