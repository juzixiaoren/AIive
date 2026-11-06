"""V62.6.1 real ORM paths: provenance closure, maintenance, source times, snapshots."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import sessionmaker

from aiive.db.forget_models import ContentProvenanceRef, ForgetShield
from aiive.db.models import Event, MemoryRecord, MemoryProposal, Thread, TurnRecord, AgentTask
from aiive.forget.phase_a_shield import execute_phase_a_shield
from aiive.forget.visibility_service import ForgetVisibilityService as Visibility
from aiive.worker.outbox_dto import ClaimedJob
from aiive.worker.job_source_guard import resolve_job_sources, blocked_source_reason


def uid():
    return str(uuid4())


def now():
    return datetime.now(timezone.utc)


def source(db):
    thread = Thread(id=uid(), title="v6261 test")
    db.add(thread)
    db.flush()
    turn = TurnRecord(id=uid(), turn_id=uid(), thread_id=thread.id, status="completed")
    db.add(turn)
    db.flush()
    event = Event(id=uid(), trace_id=uid(), thread_id=thread.id, turn_id=turn.turn_id, event_type="user_message", payload={"text": "private"})
    db.add(event)
    db.flush()
    return thread, turn, event


def memory(db, event=None):
    record = MemoryRecord(id=uid(), memory_type="fact", canonical_key="test:v6261", scope_type="global", content="private memory", lifecycle_state="active", validity_state="valid", record_version=1)
    db.add(record)
    db.flush()
    if event is not None:
        db.add(ContentProvenanceRef(owner_type="memory_record", owner_id=record.id, source_type="event", source_id=event.id))
    db.flush()
    return record


def shield(db, event):
    execute_phase_a_shield(db, mode="history_only", event_ids=[event.id], requested_by=uid())
    db.commit()


def claimed(kind, payload):
    return ClaimedJob(uid(), kind, payload, None, 0, 1, uid(), 1, "test")


def test_memory_closure_and_missing_manual_proof(db_session):
    db = db_session
    _, _, event = source(db)
    record = memory(db, event)
    orphan = memory(db)
    manual = memory(db)
    db.add(MemoryProposal(proposal_id=uid(), final_memory_id=manual.id, final_operation="create", extractor_name="manual_memory_api", execution_mode="user_required", source_event_ids=[], evidence=[]))
    db.flush()
    assert Visibility.filter_memory_ids(db, [record.id, orphan.id, manual.id]) == [record.id, manual.id]
    shield(db, event)
    assert db.query(ForgetShield).filter_by(target_type="memory_record").count() == 0
    assert Visibility.filter_memory_ids(db, [record.id, orphan.id, manual.id]) == [manual.id]
    assert record.id in Visibility.blocked_target_ids(db, "memory_record", [(record.id, now())])


def test_memory_only_does_not_reflect_to_sibling(db_session):
    db = db_session
    _, _, event = source(db)
    first, second = memory(db, event), memory(db, event)
    execute_phase_a_shield(db, mode="memory_only", memory_ids=[first.id], requested_by=uid())
    db.commit()
    # Phase A's source Event Shield exists intentionally. It must not erase
    # an independently retained sibling solely through the reverse edge.
    assert second.id in Visibility.filter_memory_ids(db, [second.id])


def test_projection_handlers_zero_calls_from_event_only_shield(db_session, monkeypatch):
    from aiive.worker import outbox_handlers as handlers
    from aiive.memory.core_memory_projection import CoreMemoryProjection
    from aiive.memory.vector_projection import MemoryVectorProjectionService
    from aiive.memory.graph_projection import GraphProjectionService
    from aiive.memory.projection import MemoryProjection
    from tests.support import persist_running_job

    db = db_session
    _, _, event = source(db)
    record = memory(db, event)
    shield(db, event)
    monkeypatch.setattr(handlers, "SessionLocal", sessionmaker(bind=db.get_bind()))
    spies = []
    for cls, method in [(CoreMemoryProjection, "refresh"), (MemoryVectorProjectionService, "refresh"), (GraphProjectionService, "refresh"), (MemoryProjection, "write_projection")]:
        spy = Mock(side_effect=AssertionError("projection must not run"))
        monkeypatch.setattr(cls, method, spy)
        spies.append(spy)
    for kind in ["core_memory_refresh", "memory_vector_refresh", "memory_graph_refresh", "memory_markdown_project"]:
        job = persist_running_job(db, job_type=kind, payload={"memory_id": record.id, "record_version": 1})
        result = getattr(handlers, "handle_" + kind)(job)
        assert result.reason.startswith("forget_source_blocked")
    for spy in spies:
        spy.assert_not_called()
    assert record.content not in MemoryProjection(db).to_markdown()


def test_maintenance_sources_and_frozen_action(db_session, monkeypatch):
    from aiive.memory.memory_store import MemoryStore
    from aiive.memory.memory_lifecycle_service import MemoryLifecycleService
    from aiive.memory.recall_config import MaintenanceConfig
    from aiive.worker.outbox_handlers import _freeze_input
    from tests.integration.backend.test_phase4_lifecycle import _freeze_action

    db = db_session
    _, _, event = source(db)
    record = memory(db, event)
    store = MemoryStore(db)
    _freeze_input(db, uid(), uid(), record, "seed", 0, now(), MaintenanceConfig(), store)
    action = _freeze_action(record, "sleep")
    shield(db, event)
    _, _, new_event = source(db)
    safe = memory(db, new_event)
    assert [r.id for r in store.select_maintenance_seeds("changed", cutoff_updated_at=now()+timedelta(days=1), cursor_time=None, cursor_id=None, limit=1, now=now())] == [safe.id]
    assert record.id not in [r.id for r in store.expand_maintenance_neighbors(safe)]
    with pytest.raises(ValueError, match="forget_source_blocked"):
        _freeze_input(db, uid(), uid(), record, "seed", 0, now(), MaintenanceConfig(), store)
    service = MemoryLifecycleService(db)
    spy = Mock(side_effect=AssertionError("must not mutate"))
    monkeypatch.setattr(service, "_apply", spy)
    assert service.apply_maintenance_action(action) == "skip_stale"
    assert action.reason_code == "forget_source_blocked"
    spy.assert_not_called()


def test_old_task_time_and_turn_sources(db_session):
    db = db_session
    thread, turn, event = source(db)
    task = AgentTask(id=uid(), thread_id=thread.id, title="old", goal="old", created_at=now()-timedelta(days=1))
    db.add(task)
    db.flush()
    execute_phase_a_shield(db, mode="history_only", thread_id=thread.id, requested_by=uid())
    db.commit()
    job = claimed("agent_task_run", {"task_id": task.id})
    assert blocked_source_reason(db, job, resolve_job_sources(db, job), now()+timedelta(days=1))
    new_task = AgentTask(id=uid(), thread_id=thread.id, title="new", goal="new", created_at=now()+timedelta(seconds=1))
    db.add(new_task)
    db.flush()
    job = claimed("agent_task_run", {"task_id": new_task.id})
    assert not blocked_source_reason(db, job, resolve_job_sources(db, job), now())
    new_task.source_turn_record_id = turn.id
    assert blocked_source_reason(db, job, resolve_job_sources(db, job), now())


@pytest.mark.parametrize("bad", [["valid", {}], ["valid", ["nested"]], {"id": "wrong"}, ["valid", 12]])
def test_malformed_references_fail_closed(db_session, bad):
    job = claimed("goal_event_dispatch", {"event_id": "valid", "source_event_ids": bad})
    assert resolve_job_sources(db_session, job).unresolved


def test_evidence_and_mixed_working_state(db_session, tmp_path):
    from aiive.runtime.working_state import WorkingStateService
    from aiive.selfdev.improvement_evidence import EvidenceCollector

    db = db_session
    thread, old_turn, event = source(db)
    _, new_turn, _ = source(db)
    task = AgentTask(id=uid(), thread_id=thread.id, source_turn_record_id=old_turn.id, title="private", goal="private", status="failed", last_error="private")
    db.add(task)
    db.flush()
    ws = WorkingStateService().get_or_create(db, thread.id)
    ws.open_loops = [{"description": "old private", "source_turn_id": old_turn.id}, {"description": "new safe", "source_turn_id": new_turn.id}]
    execute_phase_a_shield(db, mode="history_only", turn_ids=[old_turn.id], requested_by=uid())
    db.commit()
    snapshot = WorkingStateService.safe_boundary_snapshot(db, ws)
    assert [x["description"] for x in snapshot["open_loops"]] == ["new safe"]
    batch = EvidenceCollector(eval_dir=tmp_path, min_occurrences=1).collect(db)
    assert "private" not in str(batch.to_prompt_payload())
    ws.open_loops = [{"description": "unknown"}]
    with pytest.raises(ValueError, match="source_unresolved"):
        WorkingStateService.safe_boundary_snapshot(db, ws)


def test_direct_vector_and_index_rebuild_blocked(db_session, monkeypatch):
    from aiive.memory.vector_projection import MemoryVectorProjectionService
    from aiive.retrieval import index_rebuild
    from aiive.retrieval.retrieval_index import RetrievalIndexManager
    from aiive.retrieval.indexing_service import refresh_source
    from aiive.db.models import RetrievalIndexEntry
    from tests.support import persist_running_job

    db = db_session
    _, _, event = source(db)
    record = memory(db, event)
    shield(db, event)
    provider = Mock()
    factory = Mock(return_value=provider)
    assert MemoryVectorProjectionService(db, provider_factory=factory).refresh(record.id, 1) == "forget_source_blocked"
    factory.assert_not_called()
    provider.embed.assert_not_called()
    RetrievalIndexManager().create_generation(db, 1, status="active")
    assert refresh_source(db, "memory_record", record.id, "memory.created")[0] == 0
    db.commit()
    monkeypatch.setattr(index_rebuild, "SessionLocal", sessionmaker(bind=db.get_bind()))
    job = persist_running_job(db, job_type="retrieval_index_rebuild", payload={"operation_id": uid()})
    for _ in range(10):
        result = index_rebuild.handle_retrieval_index_rebuild(job)
        if result.outcome.value == "completed":
            break
        assert result.outcome.value == "continue"
    else:
        pytest.fail("index rebuild did not converge")
    assert not db.query(RetrievalIndexEntry).filter_by(source_id=record.id, is_searchable=True).count()


def test_epoch_rollover_filters_mixed_snapshot_and_blocks_unknown(db_session):
    from aiive.db.models import EpochCompactionInput
    from aiive.runtime.epoch_manager import EpochManager
    from aiive.runtime.working_state import WorkingStateService
    from tests.integration.backend._util import new_epoch, new_segment

    db = db_session
    thread, turn, event = source(db)
    epoch = new_epoch(db, thread.id)
    segment = new_segment(db, epoch.id, thread.id)
    turn.segment_id = segment.id
    turn.turn_sequence = 1
    _, old_turn, _ = source(db)
    execute_phase_a_shield(db, mode="history_only", turn_ids=[old_turn.id], requested_by=uid())
    db.commit()
    ws = WorkingStateService().get_or_create(db, thread.id)
    ws.open_loops = [{"description": "hidden", "source_turn_id": old_turn.id}, {"description": "safe", "source_turn_id": turn.id}]
    db.flush()
    result = EpochManager().begin_epoch_sealing(db, thread.id)
    assert not result.stale, result.reason
    frozen = db.get(EpochCompactionInput, result.epoch_compaction_input_id)
    assert [item["description"] for item in frozen.open_loops] == ["safe"]
    # A new active epoch exists, but unknown state must not produce another input.
    ws.open_loops = [{"description": "unknown"}]
    db.commit()
    count = db.query(EpochCompactionInput).count()
    job = claimed("epoch_rollover", {"thread_id": thread.id})
    assert resolve_job_sources(db, job).unresolved
    result = EpochManager().begin_epoch_sealing(db, thread.id)
    assert result.stale
    assert db.query(EpochCompactionInput).count() == count


def test_bad_manifest_element_is_unresolved(db_session):
    from aiive.db.models import CompactionInput
    from tests.integration.backend._util import new_epoch, new_segment

    db = db_session
    thread, turn, event = source(db)
    epoch = new_epoch(db, thread.id)
    segment = new_segment(db, epoch.id, thread.id)
    ci = CompactionInput(segment_id=segment.id, start_turn_sequence=1, end_turn_sequence=1,
                         working_state_version=1, source_hash="test", event_manifest=[{"event_id": event.id}, 5],
                         turn_manifest=[{"turn_record_id": turn.id}])
    db.add(ci)
    db.flush()
    job = claimed("segment_sealing", {"segment_id": segment.id, "compaction_input_id": ci.id})
    assert resolve_job_sources(db, job).unresolved
    for bad in ({"event_id": event.id, "turn_record_id": {}}, {"event_id": [event.id]}):
        ci.event_manifest = [{"event_id": event.id}, bad]
        assert resolve_job_sources(db, job).unresolved


def test_maintenance_skips_are_terminal_and_cursor_advances(db_session, monkeypatch):
    from aiive.db.models import MemoryMaintenanceRun, MemoryMaintenanceAction, MemoryMaintenanceInput
    from aiive.memory.recall_config import MaintenanceConfig
    from aiive.memory.memory_lifecycle_service import MemoryLifecycleService
    from aiive.worker.outbox_handlers import _create_batch_and_freeze, _execute_batch
    from tests.support import persist_running_job

    db = db_session
    _, _, event = source(db)
    record = memory(db, event)
    job = persist_running_job(db, job_type="memory_maintenance", payload={"operation_id": uid()})
    run = MemoryMaintenanceRun(operation_id=uid(), outbox_job_id=job.id, cutoff_updated_at=now()+timedelta(days=1), status="running")
    db.add(run)
    db.flush()
    shield(db, event)
    cfg = MaintenanceConfig()
    batch = _create_batch_and_freeze(db, run, "changed", cfg, now())
    assert batch is not None
    assert not db.query(MemoryMaintenanceInput).filter_by(batch_id=batch.id).count()
    action = db.query(MemoryMaintenanceAction).filter_by(batch_id=batch.id).one()
    assert action.status == "skipped_stale" and action.reason_code == "forget_source_blocked"
    spy = Mock(side_effect=AssertionError("blocked action must not execute"))
    monkeypatch.setattr(MemoryLifecycleService, "apply_maintenance_action", spy)
    _execute_batch(db, batch, run, cfg, now(), "test")
    spy.assert_not_called()
    assert batch.status == "done" and batch.skipped_stale_count == 1
    assert run.skipped_stale_count == 1 and run.cursor_id == record.id
    assert _create_batch_and_freeze(db, run, "changed", cfg, now()) is None


def test_real_manual_api_without_event_remains_visible(db_session):
    from aiive.api.routes_memories import CreateMemoryRequest, create_memory

    result = create_memory(CreateMemoryRequest(content="用户偏好深色编辑器", memory_type="fact"), db=db_session, idempotency_key=uid())
    assert result.id and result.id in Visibility.filter_memory_ids(db_session, [result.id])
    assert not db_session.query(ContentProvenanceRef).filter_by(owner_type="memory_record", owner_id=result.id).count()


def test_event_shield_evidence_never_calls_discovery_llm(db_session, tmp_path):
    from aiive.db.models import AgentRun, AgentAction, OutboxJob
    from aiive.selfdev.improvement_discovery import ImprovementDiscovery
    from aiive.selfdev.improvement_evidence import EvidenceCollector

    db = db_session
    thread, turn, event = source(db)
    task = AgentTask(id=uid(), thread_id=thread.id, source_turn_record_id=turn.id, title="private", goal="private", status="failed", last_error="private failure")
    db.add(task)
    db.flush()
    run = AgentRun(id=uid(), task_id=task.id, run_index=1, status="failed")
    db.add(run)
    db.flush()
    db.add(AgentAction(task_id=task.id, run_id=run.id, sequence=1, capability_id="test",
                       arguments_hash="h", descriptor_hash="h", idempotency_key=uid(),
                       status="failed", error_message="private failure"))
    db.add(OutboxJob(job_type="agent_task_run", operation_id=uid(), payload={"task_id": task.id}, status="deadletter", error_message="private failure"))
    shield(db, event)
    llm = Mock()
    batch = EvidenceCollector(eval_dir=tmp_path, min_occurrences=1).collect(db)
    assert not batch.groups
    assert batch.scanned["agent_task_blocked"] == 1
    assert batch.scanned["agent_action_blocked"] == 1
    assert batch.scanned["outbox_job_blocked"] >= 1
    ImprovementDiscovery(llm, collector=EvidenceCollector(eval_dir=tmp_path, min_occurrences=1)).run(db=db, now=now())
    llm.chat.assert_not_called()


def test_visibility_query_failure_closes_entire_batch(monkeypatch):
    db = Mock()
    db.query.side_effect = RuntimeError("unavailable")
    assert Visibility.blocked_target_ids(db, "memory_record", [("a", None), ("b", None)]) == {"a", "b"}


def test_historical_persisted_batch_rechecked_before_mutation(db_session, monkeypatch):
    from aiive.db.models import MemoryMaintenanceRun, MemoryMaintenanceInput
    from aiive.memory.recall_config import MaintenanceConfig
    from aiive.memory.memory_lifecycle_service import MemoryLifecycleService
    from aiive.worker.outbox_handlers import _create_batch_and_freeze, _execute_batch
    from tests.integration.backend.test_phase4_lifecycle import _freeze_action
    from tests.support import persist_running_job

    db = db_session
    _, _, event = source(db)
    record = memory(db, event)
    version = record.record_version
    job = persist_running_job(db, job_type="memory_maintenance", payload={"operation_id": uid()})
    run = MemoryMaintenanceRun(operation_id=uid(), outbox_job_id=job.id, cutoff_updated_at=now()+timedelta(days=1), status="running")
    db.add(run)
    db.flush()
    cfg = MaintenanceConfig()
    batch = _create_batch_and_freeze(db, run, "changed", cfg, now())
    frozen = db.query(MemoryMaintenanceInput).filter_by(batch_id=batch.id).one()
    assert frozen.snapshot_json["content"] == record.content
    action = _freeze_action(record, "sleep", run_id=run.id, batch_id=batch.id)
    action.source_input_ids = [frozen.id]
    action.operation_group_id = uid()
    action.idempotency_key = uid()
    db.add(action)
    batch.status = "planned"
    db.commit()
    shield(db, event)
    spy = Mock(side_effect=AssertionError("historical batch must not reach mutation service"))
    monkeypatch.setattr(MemoryLifecycleService, "apply_maintenance_action", spy)
    _execute_batch(db, batch, run, cfg, now(), "test")
    db.commit()
    spy.assert_not_called()
    assert db.get(MemoryRecord, record.id).record_version == version
    assert action.status == "skipped_stale" and action.reason_code == "forget_source_blocked"
    assert batch.status == "done" and batch.skipped_stale_count == 1


def test_epoch_rollover_handler_event_shield_creates_no_snapshot(db_session, monkeypatch):
    from aiive.db.models import EpochCompactionInput, CompactionInput
    from aiive.runtime.epoch_manager import EpochManager
    from aiive.worker import outbox_handlers
    from tests.integration.backend._util import new_epoch, new_segment
    from tests.support import persist_running_job

    db = db_session
    thread, turn, event = source(db)
    epoch = new_epoch(db, thread.id)
    segment = new_segment(db, epoch.id, thread.id)
    turn.segment_id = segment.id
    db.flush()
    shield(db, event)
    job = persist_running_job(db, job_type="epoch_rollover", payload={"thread_id": thread.id})
    monkeypatch.setattr(outbox_handlers, "SessionLocal", sessionmaker(bind=db.get_bind()))
    freeze = Mock(side_effect=AssertionError("source guard must run before snapshot creation"))
    monkeypatch.setattr(EpochManager, "begin_epoch_sealing", freeze)
    llm = Mock()
    monkeypatch.setattr(outbox_handlers, "_get_llm_client", llm)
    result = outbox_handlers.handle_epoch_rollover(job)
    assert result.reason.startswith("forget_source_blocked")
    freeze.assert_not_called()
    llm.assert_not_called()
    assert db.query(EpochCompactionInput).count() == db.query(CompactionInput).count() == 0


def test_maintenance_clone_inherits_event_sources(db_session):
    from aiive.memory.memory_lifecycle_service import MemoryLifecycleService
    from tests.integration.backend.test_phase4_lifecycle import _freeze_action

    db = db_session
    _, _, event = source(db)
    winner = memory(db, event)
    _, _, other_event = source(db)
    loser = memory(db, other_event)
    action = _freeze_action(winner, "supersede", related=loser)
    assert MemoryLifecycleService(db).apply_maintenance_action(action) == "applied"
    db.flush()
    clone = db.get(MemoryRecord, winner.superseded_by)
    assert clone.id in Visibility.filter_memory_ids(db, [clone.id])
    refs = db.query(ContentProvenanceRef).filter_by(owner_type="memory_record", owner_id=clone.id).all()
    assert {ref.source_id for ref in refs} == {event.id}
    # The losing value is not a source of the copied winning value.
    shield(db, other_event)
    assert clone.id in Visibility.filter_memory_ids(db, [clone.id])
    shield(db, event)
    assert clone.id not in Visibility.filter_memory_ids(db, [clone.id])


def test_write_merge_inherits_all_event_sources(db_session):
    from aiive.memory.memory_write_service import MemoryWriteService
    from aiive.memory.memory_types import MemoryProposal as Proposal
    from aiive.memory.memory_gate import GateDecision
    from aiive.memory.conflict_resolver import ResolutionResult

    db = db_session
    _, _, event = source(db)
    first = memory(db, event)
    _, _, other_event = source(db)
    second = memory(db, other_event)
    proposal = Proposal(memory_type="fact", canonical_key="test:v6261", content="merged", confidence=0.95)
    result = MemoryWriteService(db)._execute_merge(
        proposal,
        ResolutionResult(operation="merge", existing_records=[first, second], reason="merge"),
        GateDecision(decision="active", reason="test"),
        "",
    )
    db.flush()
    assert result.memory_id in Visibility.filter_memory_ids(db, [result.memory_id])
    refs = db.query(ContentProvenanceRef).filter_by(owner_type="memory_record", owner_id=result.memory_id).all()
    assert {ref.source_id for ref in refs} == {event.id, other_event.id}
    shield(db, event)
    assert result.memory_id not in Visibility.filter_memory_ids(db, [result.memory_id])
