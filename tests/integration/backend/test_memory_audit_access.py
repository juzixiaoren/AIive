"""Real-ORM regression for the memory audit; no external model calls."""
import pytest
from tests.integration.backend.test_forget_v6261 import source, memory, shield
from aiive.memory.memory_store import MemoryStore
from aiive.memory.memory_read_model import MemoryReadModel
from aiive.memory.memory_lifecycle_service import MemoryLifecycleService
from aiive.memory.memory_write_service import MemoryWriteService
from aiive.memory.memory_types import MemoryProposal, EvidenceItem
from aiive.context.run_context import RunContext
from aiive.db.models import MemoryRecord, MemoryLineage
from aiive.api.routes_personal import list_personal_signals
from aiive.forget.durable_sources import persist_sources, sources_visible


def test_identity_and_policy_ignore_event_shield(db_session):
    _, _, event = source(db_session)
    rec = memory(db_session, event)
    rec.canonical_key = "user.display_name"
    rec.memory_type = "user_profile"
    policy = memory(db_session, event)
    policy.canonical_key = "policy.test"
    db_session.flush()
    reader = MemoryReadModel(MemoryStore(db_session))
    assert reader.resolve_identity().user_display_name == rec.content
    assert reader.resolve_policies()
    shield(db_session, event)
    assert reader.resolve_identity().user_display_name == ""
    assert reader.resolve_policies() == []
    assert list_personal_signals(db_session) == []


@pytest.mark.parametrize("state", ["active", "candidate", "sleeping"])
def test_new_event_creates_independent_fact(db_session, state):
    _, _, event = source(db_session)
    old = memory(db_session, event)
    old.lifecycle_state = state
    old.canonical_key = "user.display_name"
    old.memory_type = "user_profile"
    db_session.flush()
    shield(db_session, event)
    thread, turn, new_event = source(db_session)
    proposal = MemoryProposal(memory_type="user_profile", canonical_key=old.canonical_key,
        content="new independent identity", confidence=0.99, source_event_ids=[new_event.id],
        evidence=[EvidenceItem(source_event_id=new_event.id, source_type="user_message", trust_level="trusted")])
    result = MemoryWriteService(db_session).write(proposal, RunContext(thread_id=thread.id, trace_id="audit"))
    assert result.written and result.operation == "create"
    assert result.memory_id != old.id
    assert old.lifecycle_state == state
    assert db_session.query(MemoryLineage).filter_by(predecessor_id=old.id, successor_id=result.memory_id).count() == 0


@pytest.mark.parametrize("state,action", [("sleeping", "wake"), ("candidate", "promote")])
def test_direct_actions_cannot_revive_blocked_source(db_session, state, action):
    _, _, event = source(db_session)
    rec = memory(db_session, event)
    rec.lifecycle_state = state
    db_session.flush()
    shield(db_session, event)
    result = getattr(MemoryLifecycleService(db_session), action)(rec.id)
    assert not (result[0] if isinstance(result, tuple) else result)
    assert rec.lifecycle_state == state


def test_invalid_candidates_excluded(db_session):
    _, _, event = source(db_session)
    rec = memory(db_session, event)
    rec.validity_state = "superseded"
    db_session.flush()
    assert not MemoryStore(db_session).get_active_by_key_scope_locked(rec.canonical_key, rec.scope_type, rec.scope_id)


def test_durable_dependencies_fail_closed_and_preserve_all_sources(db_session):
    _, _, event = source(db_session)
    _, _, other = source(db_session)
    persist_sources(db_session, "task_evidence", "audit", {"source_event_ids": [event.id, other.id]})
    db_session.flush()
    assert sources_visible(db_session, "task_evidence", "audit")
    assert not sources_visible(db_session, "task_evidence", "unknown")
    shield(db_session, event)
    assert not sources_visible(db_session, "task_evidence", "audit")


def test_visible_pagination_skips_blocked_first_batch(db_session):
    _, _, event = source(db_session)
    for i in range(40):
        memory(db_session, event)
    shield(db_session, event)
    _, _, fresh = source(db_session)
    rec = memory(db_session, fresh)
    assert rec.id in {r.id for r in MemoryStore(db_session).list_all(limit=1)}


def test_source_turn_fields_round_trip(db_session):
    from aiive.db.models import MemoryProposal as StoredProposal
    thread, turn, event = source(db_session)
    proposal = MemoryProposal(memory_type="user_profile", canonical_key="user.display_name",
        content="fresh", confidence=0.99, source_event_ids=[event.id],
        source_turn_id=turn.turn_id, source_turn_record_id=turn.id,
        evidence=[EvidenceItem(source_event_id=event.id, source_type="user_message", trust_level="trusted")])
    result = MemoryWriteService(db_session).write(proposal, RunContext(thread_id=thread.id, trace_id="audit"))
    assert result.written
    stored = db_session.query(StoredProposal).filter_by(proposal_id=proposal.proposal_id).one()
    assert stored.source_turn_id == turn.turn_id
    assert stored.source_turn_record_id == turn.id


def test_integrity_audit_does_not_return_content(db_session):
    from aiive.memory.integrity_audit import audit_memory_integrity
    import json
    rec = memory(db_session)
    rec.content = "unique secret marker"
    db_session.flush()
    report = audit_memory_integrity(db_session)
    assert report["findings"]
    assert rec.content not in json.dumps(report)


def test_forget_watermark_invalidates_context(db_session):
    from aiive.forget.concurrency import forget_revision
    _, _, event = source(db_session)
    prior = forget_revision(db_session)
    shield(db_session, event)
    assert forget_revision(db_session) != prior


def test_identity_excludes_other_thread_scope(db_session):
    thread, _, event = source(db_session)
    rec = memory(db_session, event)
    rec.canonical_key = "user.display_name"
    rec.scope_type, rec.scope_id = "thread", thread.id
    db_session.flush()
    store = MemoryStore(db_session)
    assert MemoryReadModel(store).resolve_identity().user_display_name == ""
    assert MemoryReadModel(store, [("thread", thread.id), ("global", None)]).resolve_identity().user_display_name == rec.content


def test_relation_names_do_not_inherit_unproved_shared_aliases(db_session):
    import json
    from tests.unit.backend.test_memory_graph import _record, _fact, _refresh
    from aiive.db.models import GraphEntity
    from aiive.api.routes_memories import get_memory_relations
    rec = _record(db_session, facts=[_fact(predicate="works_at", object_="AIive")],
                  key="identity.company", content="I work at AIive")
    _refresh(db_session, rec)
    for entity in db_session.query(GraphEntity).all():
        entity.display_name = "forgotten entity marker"
        entity.aliases = ["forgotten entity marker"]
    db_session.flush()
    payload = get_memory_relations(rec.id, db_session)
    assert payload["relations"]
    assert "forgotten entity marker" not in json.dumps(payload)


def test_conflict_question_follows_secret_redaction(db_session):
    import json
    from tests.integration.backend.test_memory_conflict_confirmation import _new_thread, _seed_record, _proposal, _park_case
    from aiive.api.routes_memories import list_memory_conflicts
    thread_id = _new_thread(db_session)
    old = _seed_record(db_session, _proposal("user.display_name", "old"))
    case = _park_case(db_session, thread_id=thread_id, proposal=_proposal("user.display_name", "new"), old=old)
    case.sensitivity = "secret"
    case.question = "secret conflict question marker"
    db_session.flush()
    payload = list_memory_conflicts(db=db_session)
    assert payload
    assert case.question not in json.dumps(payload)


def test_evidence_capture_rechecks_sources_before_writing(db_session):
    from types import SimpleNamespace
    from aiive.task_runtime.evidence import EvidenceStore
    from aiive.db.models import TaskEvidence
    _, _, event = source(db_session)
    shield(db_session, event)
    action = SimpleNamespace(arguments={"source_event_id": event.id})
    with pytest.raises(ValueError, match="forget_source_blocked"):
        EvidenceStore().capture_result(db_session, task_id="missing", run_id="audit", action=action, result={"text": "private"})
    assert db_session.query(TaskEvidence).count() == 0


def test_goal_final_prompt_and_send_time_revalidation(db_session):
    from tests.unit.backend.test_goal_event_sources import _confirmed, _plan, FakeLLM
    from aiive.db.models import AgentTask
    from aiive.task_runtime.goal_service import GoalService
    from aiive.task_runtime.goal_decision import GoalDecisionEngine
    thread, _, event = source(db_session)
    rec = memory(db_session, event)
    rec.content = "goal private marker"
    task_id = _confirmed(db_session, thread.id, _plan([{"id": "next", "title": "next", "kind": "autonomous"}]))
    task = db_session.get(AgentTask, task_id)
    plan = GoalService(db_session).load_plan(task)
    fake = FakeLLM()
    engine = GoalDecisionEngine(db_session, fake)
    context = engine.context(task, plan, plan.steps[0], {})
    messages = engine.messages(context)
    assert rec.content in str(messages)
    shield(db_session, event)
    decision, _ = engine.decide(messages, trace_id="audit")
    assert decision.action == "no_action" and not fake.calls
    context = engine.context(task, plan, plan.steps[0], {})
    engine.decide(engine.messages(context), trace_id="audit2")
    assert len(fake.calls) == 1
    assert "goal private marker" not in str(fake.calls)


@pytest.mark.parametrize("summary_mode", [True, False])
def test_sealing_bridge_filters_real_source_closure(db_session, summary_mode):
    from aiive.db.models import SegmentSummary, CompactionInput
    from aiive.runtime.context_assembler import ContextAssembler
    from tests.integration.backend._util import new_epoch, new_segment
    thread, turn, event = source(db_session)
    marker = "sealing private marker"
    event.payload = {"content": marker}
    epoch = new_epoch(db_session, thread.id)
    segment = new_segment(db_session, epoch.id, thread.id, status="sealing")
    if summary_mode:
        summary = SegmentSummary(segment_id=segment.id, goal=marker, source_event_ids=[event.id])
        db_session.add(summary)
        db_session.flush()
        segment.summary_id = summary.id
    else:
        db_session.add(CompactionInput(segment_id=segment.id, start_turn_sequence=1, end_turn_sequence=1,
            working_state_version=1, source_hash="audit", event_manifest=[{"event_id": event.id}], turn_manifest=[]))
    db_session.flush()
    assembler = ContextAssembler(token_counter=None)
    assert marker in assembler._load_sealing_bridge(db_session, thread.id)
    shield(db_session, event)
    assert marker not in assembler._load_sealing_bridge(db_session, thread.id)


def test_summary_and_checkpoint_share_event_closure(db_session):
    from aiive.db.models import SegmentSummary, EpochCheckpoint
    from aiive.forget.visibility_service import ForgetVisibilityService as Visibility
    from tests.integration.backend._util import new_epoch, new_segment
    thread, _, event = source(db_session)
    epoch = new_epoch(db_session, thread.id)
    segment = new_segment(db_session, epoch.id, thread.id)
    summary = SegmentSummary(segment_id=segment.id, goal="private", source_event_ids=[event.id])
    checkpoint = EpochCheckpoint(epoch_id=epoch.id, current_goal="private", source_segment_ids=[segment.id])
    db_session.add_all([summary, checkpoint])
    db_session.flush()
    assert not Visibility.blocked_target_ids(db_session, "segment_summary", [(summary.id, None)])
    assert not Visibility.blocked_target_ids(db_session, "epoch_checkpoint", [(checkpoint.id, None)])
    shield(db_session, event)
    assert summary.id in Visibility.blocked_target_ids(db_session, "segment_summary", [(summary.id, None)])
    assert checkpoint.id in Visibility.blocked_target_ids(db_session, "epoch_checkpoint", [(checkpoint.id, None)])
