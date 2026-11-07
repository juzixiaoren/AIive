"""Audit follow-up: independent sources and serialized copies, using real ORM rows."""

import json
from datetime import timedelta
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from aiive.db.forget_models import ContentProvenanceRef
from aiive.db.models import AgentAction, AgentTaskEvent, TaskArtifact, Event, OutboxJob
from aiive.task_runtime.repository import TaskRepository
from aiive.task_runtime.read_repository import (
    TaskReadRepository,
    TaskContentUnavailable,
)
from aiive.task_runtime.evidence import EvidenceStore, ArtifactStore
from tests.integration.backend.test_forget_v6261 import source, memory, shield, uid, now


def task_result(db, monkeypatch, *, external=False):
    from aiive.storage.object_store import ObjectRef
    from aiive.task_runtime import evidence as module

    thread, turn, original = source(db)
    independent = Event(
        thread_id=thread.id,
        trace_id=uid(),
        event_type="user_message",
        payload={"text": "E2"},
    )
    db.add(independent)
    db.flush()
    repo = TaskRepository(db)
    task = repo.create_task(
        thread_id=thread.id, goal="E1", source_turn_record_id=turn.id, enqueue=False
    )
    task, run = repo.start_run(task.id, trigger="audit")
    action = repo.create_action(
        task=task,
        run=run,
        capability_id="get_current_time",
        arguments={},
        descriptor_hash="test",
        risk_level="low",
        requires_approval=False,
    )
    monkeypatch.setattr(
        module,
        "put_content_addressed",
        lambda *a, **kw: (ObjectRef("test", uid()), "digest"),
    )
    marker = "INDEPENDENT_PRIVATE_CONTENT"
    if external:
        marker *= 10000
    result = {
        "ok": True,
        "targeted_test_report": {"ok": True},
        "source_event_id": independent.id,
        "private": marker,
        "artifacts": [
            {"name": "private.txt", "content": marker, "summary": marker[:100]}
        ],
    }
    evidence = EvidenceStore().capture_result(
        db, task_id=task.id, run_id=run.id, action=action, result=result
    )
    event = repo.append_event(
        task.id, "action_result", {"private": marker[:100]}, action_id=action.id
    )
    artifact = db.query(TaskArtifact).filter_by(action_id=action.id).one()
    monkeypatch.setattr(module, "get_range", lambda ref, *, offset, max_bytes: json.dumps(result).encode()[offset:offset + max_bytes])
    db.commit()
    return repo, task, action, evidence, artifact, independent, original, event


@pytest.mark.parametrize("external", [False, True])
def test_evidence_artifact_and_task_copies_block_before_object_reads(
    db_session, monkeypatch, external
):
    from aiive.task_runtime import evidence as module
    from aiive.api.routes_agent_tasks import read_task_artifact, read_task_evidence
    from aiive.task_runtime.context_assembler import TaskContextAssembler
    from aiive.tools.registry import ToolRegistry

    db = db_session
    repo, task, action, evidence, artifact, event, origin, task_event = task_result(
        db, monkeypatch, external=external
    )
    assert TaskReadRepository(db).task_visible(task)
    assert origin.id != event.id
    from aiive.control.capability_broker import CapabilityBroker

    task.task_type = "selfdev"
    assert CapabilityBroker(db)._lifecycle_block(task, "promote_slot", {}) is None
    shield(db, event)
    spy = Mock(
        side_effect=AssertionError("authorization must precede external byte read")
    )
    monkeypatch.setattr(module, "get_range", spy)
    assert (
        CapabilityBroker(db)._lifecycle_block(task, "promote_slot", {})
        == "promotion_requires_passing_isolated_evaluation"
    )
    assert TaskReadRepository(db).task_visible(task)  # E1 remains visible.
    for store, item in ((EvidenceStore, evidence), (ArtifactStore, artifact)):
        with pytest.raises(TaskContentUnavailable):
            store.read(db, item, 0, 1024)
    for route, item in ((read_task_evidence, evidence), (read_task_artifact, artifact)):
        with pytest.raises(HTTPException) as error:
            route(task.id, item.id, offset=0, max_bytes=1024, db=db)
        assert error.value.status_code == 404
    detail = repo.detail(task)
    assert detail["evidence"] == detail["artifacts"] == detail["actions"] == []
    assert task_event.id not in [
        row.id for row in TaskReadRepository(db).events(task.id)
    ]
    snapshot = TaskContextAssembler(db).snapshot(
        task, [], registry=ToolRegistry(), discovery_registry=ToolRegistry()
    )
    assert "INDEPENDENT_PRIVATE_CONTENT" not in str(snapshot)
    from aiive.runtime.turn_events import _task_snapshot
    assert "INDEPENDENT_PRIVATE_CONTENT" not in str(_task_snapshot(db, task.id))
    spy.assert_not_called()


def test_cascade_discovers_and_scrubs_independent_result_copies(
    db_session, monkeypatch
):
    from aiive.forget.phase_a_shield import execute_phase_a_shield
    from aiive.forget.content_scope import load_scope
    from aiive.forget.content_scrub import scrub_task_runtime

    db = db_session
    repo, task, action, evidence, artifact, event, origin, task_event = task_result(
        db, monkeypatch
    )
    task.task_state = {**task.task_state, "private": "INDEPENDENT_PRIVATE_CONTENT"}
    checkpoint = repo.checkpoint(task.id, reason="derived")
    result = execute_phase_a_shield(
        db, mode="history_only", event_ids=[event.id], requested_by=uid()
    )
    db.commit()
    scope = load_scope(db, result["operation_id"], "history_only")
    assert scope is not None
    for item, ids in (
        (evidence, scope.task_evidence_ids),
        (artifact, scope.task_artifact_ids),
        (action, scope.agent_action_ids),
        (task_event, scope.agent_task_event_ids),
        (checkpoint, scope.agent_task_checkpoint_ids),
    ):
        assert item.id in ids
    assert task.id in scope.agent_task_ids
    scrub_task_runtime(db, scope)
    db.expire_all()
    assert db.get(AgentAction, action.id).result_summary is None
    assert db.get(AgentTaskEvent, task_event.id).payload == {}
    assert db.get(Event, origin.id).payload  # independent original history survives


def test_task_snapshot_invalidated_before_model_send(db_session, monkeypatch):
    from aiive.task_runtime.context_assembler import TaskContextAssembler
    from aiive.task_runtime.agent_executor import TaskAgentExecutor
    from aiive.tools.registry import ToolRegistry

    db = db_session
    _, task, _, _, _, event, _, _ = task_result(db, monkeypatch)
    original = TaskContextAssembler.messages
    monkeypatch.setattr(TaskContextAssembler, "_registries", lambda *a: (ToolRegistry(), ToolRegistry()))

    def snapshot_then_forget(assembler, *args, **kwargs):
        result = original(assembler, *args, **kwargs)
        assert "INDEPENDENT_PRIVATE_CONTENT" in str(result)
        shield(db, event)
        return result

    monkeypatch.setattr(TaskContextAssembler, "messages", snapshot_then_forget)
    llm = Mock()
    with pytest.raises(TaskContentUnavailable):
        TaskAgentExecutor(db, llm).decide(task, [])
    llm.chat.assert_not_called()


def test_goal_question_modify_keeps_real_input_proof_and_blocks_current_plan(
    db_session,
):
    from aiive.task_runtime.goal_service import GoalService
    from aiive.task_runtime.context_assembler import TaskContextAssembler
    from aiive.task_runtime.agent_executor import TaskAgentExecutor
    from tests.unit.backend.test_long_term_goal import _plan, _propose

    db = db_session
    thread, _, original = source(db)
    task = _propose(db, thread, _plan())
    goals = GoalService(db)
    goals.confirm(task.id)
    edit = Event(
        thread_id=thread.id,
        trace_id=uid(),
        event_type="user_message",
        payload={"text": "E2 plan edit"},
    )
    db.add(edit)
    db.flush()
    goals.answer_step_question(
        task.id,
        "resume",
        answer="change",
        intent="modify_plan",
        plan_changes={"summary": "INDEPENDENT_PLAN"},
        user_event_id=edit.id,
    )
    db.commit()
    assert (
        db.query(ContentProvenanceRef)
        .filter_by(owner_type="goal_plan", owner_id=task.id, source_id=edit.id)
        .count()
    )
    changed = (
        db.query(AgentTaskEvent)
        .filter_by(task_id=task.id, event_type="goal_modified")
        .one()
    )
    assert changed.payload["user_event_id"] == edit.id
    shield(db, edit)
    assert goals.load_plan(task) is None
    assert TaskReadRepository(db).get_task(task.id) is None
    with pytest.raises(TaskContentUnavailable):
        TaskContextAssembler(db).snapshot(task, [])
    llm = Mock()
    with pytest.raises(TaskContentUnavailable):
        TaskAgentExecutor(db, llm).decide(task, [])
    llm.chat.assert_not_called()
    assert db.get(Event, original.id).payload


@pytest.mark.parametrize("forget_before_apply", [False, True])
def test_model_decision_sources_survive_a_new_engine(db_session, forget_before_apply):
    from aiive.task_runtime.goal_decision import GoalDecisionEngine, GoalDecision, persist_decision
    from aiive.task_runtime.goal_service import GoalService
    from tests.unit.backend.test_long_term_goal import _plan, _propose

    db = db_session
    thread, _, _ = source(db)
    task = _propose(db, thread, _plan())
    GoalService(db).confirm(task.id)
    edit = Event(thread_id=thread.id, trace_id=uid(), event_type="user_message", payload={"text": "model input E2"})
    db.add(edit)
    db.flush()
    decision = GoalDecision(action="modify_plan", plan_changes={"summary": "derived E2"})
    decision_id = persist_decision(db, task_id=task.id, step_id="resume", trigger_key="audit",
        decision=decision, meta={"source_references": {"source_event_ids": [edit.id]}})
    db.commit()
    if forget_before_apply:
        shield(db, edit)
    outcome = GoalDecisionEngine(db).apply(task_id=task.id, step_id="resume", trigger_key="audit",
        decision=decision, decision_id=decision_id)
    if forget_before_apply:
        assert outcome["status"] == "rejected"
    else:
        assert outcome["status"] != "rejected"
        assert db.query(ContentProvenanceRef).filter_by(owner_type="goal_plan", owner_id=task.id, source_id=edit.id).count()
        db.commit()
        shield(db, edit)
        assert GoalService(db).load_plan(task) is None


def test_identity_unset_is_independent_and_can_be_set_again(db_session):
    from aiive.memory.memory_read_model import MemoryReadModel
    from aiive.memory.memory_store import MemoryStore
    from aiive.runtime.agent_graph import _build_runtime_identity

    db = db_session
    _, _, e1 = source(db)
    _, _, e2 = source(db)
    display, full = memory(db, e1), memory(db, e2)
    display.canonical_key, display.content = "user.display_name", "nickname"
    full.canonical_key, full.content = "user.name", "full name"
    db.flush()
    reader = MemoryReadModel(MemoryStore(db))
    assert reader.resolve_identity().to_slots()["user.display_name"]["state"] == "set"
    shield(db, e1)
    identity = reader.resolve_identity()
    assert identity.user_name == "full name"
    assert identity.to_slots()["user.display_name"] == {
        "state": "unset",
        "value": None,
        "source_memory_id": None,
    }
    assert "user_display_name: [暂未设置]" in _build_runtime_identity(
        identity.to_dict()
    )
    _, _, e3 = source(db)
    replacement = memory(db, e3)
    replacement.canonical_key, replacement.content = "user.display_name", "new nickname"
    db.flush()
    assert (
        reader.resolve_identity().to_slots()["user.display_name"]["source_memory_id"]
        == replacement.id
    )
    assert all(r.content != "[暂未设置]" for r in (display, full, replacement))


def test_scope_priority_precedes_timestamp(db_session):
    from aiive.memory.memory_read_model import MemoryReadModel
    from aiive.memory.memory_store import MemoryStore

    db = db_session
    thread, _, event = source(db)
    local, global_ = memory(db, event), memory(db, event)
    local.canonical_key = global_.canonical_key = "user.display_name"
    local.scope_type, local.scope_id, local.content = (
        "thread",
        thread.id,
        "thread alias",
    )
    local.updated_at = now() - timedelta(days=1)
    global_.content, global_.updated_at = "global alias", now()
    db.flush()
    assert (
        MemoryReadModel(MemoryStore(db), [("thread", thread.id), ("global", None)])
        .resolve_identity()
        .user_display_name
        == "thread alias"
    )


def test_core_cache_hit_ignores_projection_generation_but_checks_sources(
    db_session, monkeypatch
):
    from aiive.memory import core_memory_projection as core

    db = db_session
    _, _, event = source(db)
    record = memory(db, event)
    record.canonical_key, record.content = "user.display_name", "visible alias"
    db.flush()
    core.CoreMemoryProjection.refresh(db)
    core.CoreMemoryProjection.refresh(db)
    spy = Mock(wraps=core.build_blocks)
    monkeypatch.setattr(core, "build_blocks", spy)
    loaded = core.load_core_memory(db)
    assert loaded and loaded[0].projection_version >= 2
    spy.assert_not_called()
    record.content, record.record_version = "changed alias", record.record_version + 1
    db.flush()
    assert "changed alias" in str(core.load_core_memory(db))
    assert spy.call_count == 1
    record.sensitivity = "secret"
    db.flush()
    assert "changed alias" not in str(core.load_core_memory(db))


def test_sleep_archive_do_not_mutate_hidden_source(db_session):
    from aiive.memory.memory_lifecycle_service import MemoryLifecycleService

    db = db_session
    _, _, event = source(db)
    record = memory(db, event)
    shield(db, event)
    original = (
        record.lifecycle_state,
        record.record_version,
        db.query(OutboxJob).count(),
        db.query(Event).count(),
    )
    service = MemoryLifecycleService(db)
    assert service.sleep(record.id) == (False, "forget_source_blocked")
    assert service.archive(record.id) == (False, "forget_source_blocked")
    assert (
        record.lifecycle_state,
        record.record_version,
        db.query(OutboxJob).count(),
        db.query(Event).count(),
    ) == original


@pytest.mark.parametrize(
    "hidden", ["shield", "secret", "sleeping", "invalid", "other_scope"]
)
def test_graph_peers_require_visible_active_authorized_sources(db_session, hidden):
    from aiive.memory.graph_projection import GraphProjectionService
    from aiive.memory.graph_read_repository import GraphReadRepository
    from aiive.db.models import GraphRelation

    db = db_session
    _, _, event = source(db)
    record = memory(db, event)
    record.graph_facts = [
        {
            "subject": "user",
            "predicate": "works_at",
            "object": "private co",
            "source_span": "private",
            "confidence": 0.9,
        }
    ]
    db.flush()
    GraphProjectionService(db).refresh(record.id, record.record_version)
    relation = db.query(GraphRelation).filter_by(source_memory_id=record.id).one()
    reader = GraphReadRepository(db)
    assert reader.current_peers({relation.subject_entity_id}, [("global", None)])
    if hidden == "shield":
        shield(db, event)
    elif hidden == "secret":
        record.sensitivity = "secret"
    elif hidden == "sleeping":
        record.lifecycle_state = "sleeping"
    elif hidden == "invalid":
        record.validity_state = "disputed"
    else:
        record.scope_type, record.scope_id = "thread", uid()
    db.flush()
    assert reader.current_peers({relation.subject_entity_id}, [("global", None)]) == []


@pytest.mark.parametrize("field", ["inputs", "last_decision", "report"])
def test_legacy_merged_task_state_needs_its_own_proof(db_session, field):
    from aiive.db.models import AgentTask
    db = db_session
    thread, turn, _ = source(db)
    task = AgentTask(thread_id=thread.id, source_turn_record_id=turn.id, title="legacy", goal="E1")
    if field == "report":
        task.report = {"summary": "unproven independent source"}
    else:
        task.task_state = {field: ["unproven independent source"]}
    db.add(task)
    db.flush()
    assert TaskReadRepository(db).get_task(task.id) is None
