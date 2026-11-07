"""Forget before Cascade must block questions, Inbox injection and trigger firing."""
import pytest

from aiive.db.forget_models import ContentProvenanceRef
from aiive.db.models import AgentTaskEvent, Event, OutboxJob, TurnEventInbox
from aiive.forget.durable_sources import persist_sources
from aiive.forget.phase_a_shield import execute_phase_a_shield
from aiive.runtime.turn_events import build_internal_context, claim_pending_events, enqueue_turn_event
from aiive.task_runtime.goal_events import GoalEvent
from aiive.task_runtime.goal_questions import list_open_questions, load_question, resolve_question
from aiive.task_runtime.goal_service import GoalService
from aiive.task_runtime.goal_trigger_service import GoalTriggerService
from aiive.task_runtime.read_repository import TaskReadRepository
from aiive.task_runtime.repository import TaskRepository
from tests.integration.backend.test_forget_v6261 import source, uid
from tests.unit.backend.test_goal_question_state_consistency import _ask, _assemble
from tests.unit.backend.test_turn_event_injection import _running_turn


def goal(db, *, event_type=None):
    thread, turn, original = source(db)
    task = TaskRepository(db).create_task(thread_id=thread.id, goal="independent goal",
        task_type="long_term_goal", source_turn_record_id=turn.id, enqueue=False)
    step = {"id": "next", "title": "next", "kind": "autonomous"}
    if event_type:
        step["trigger"] = {"kind": "event_condition", "event_types": [event_type]}
    service = GoalService(db)
    service.attach_plan(task, {"summary": "goal", "steps": [step]})
    service.confirm(task.id)
    db.commit()
    return thread, task, original


def signal(db, thread_id, marker="PRIVATE_EVENT", event_type="runtime_signal"):
    event = Event(thread_id=thread_id, trace_id=uid(), event_type=event_type, payload={"text": marker})
    db.add(event)
    db.flush()
    return event


def forget(db, event):
    result = execute_phase_a_shield(db, mode="history_only", event_ids=[event.id], requested_by=uid())
    db.commit()
    return result["operation_id"]


@pytest.mark.parametrize("origin", ["task", "question", "plan"])
def test_forgotten_question_absent_from_list_direct_load_and_prompt(db_session, monkeypatch, origin):
    db = db_session
    thread, task, original = goal(db)
    question_id = _ask(db, task.id, "next", "PRIVATE_QUESTION")["question_id"]
    if origin != "task":
        original = signal(db, thread.id)
        if origin == "question":
            event = db.query(AgentTaskEvent).filter_by(task_id=task.id, event_type="goal_question_asked").one()
            persist_sources(db, "agent_task_event", event.id, {"source_event_id": original.id})
        else:
            persist_sources(db, "goal_plan", task.id, {"source_event_id": original.id})
        db.commit()
    assert [q.question_id for q in list_open_questions(db, thread.id)] == [question_id]
    assert load_question(db, question_id).is_open
    assert "PRIVATE_QUESTION" in str(_assemble(db, monkeypatch, thread.id).messages)
    forget(db, original)
    if origin == "question":
        assert TaskReadRepository(db).get_task(task.id) is not None
    assert list_open_questions(db, thread.id) == []
    assert load_question(db, question_id) is None
    assembled = _assemble(db, monkeypatch, thread.id)
    assert assembled.agent_ctx["open_questions"] == []
    assert "PRIVATE_QUESTION" not in str(assembled.messages)


def test_hidden_resolution_answer_not_returned_by_direct_question_load(db_session):
    db = db_session
    thread, task, _ = goal(db)
    question_id = _ask(db, task.id, "next", "visible question")["question_id"]
    question = load_question(db, question_id)
    answer = signal(db, thread.id, "PRIVATE_ANSWER", "user_message")
    resolve_question(db, question=question, user_event_id=answer.id, answer="PRIVATE_ANSWER", intent="answer")
    db.commit()
    assert load_question(db, question_id).answer == "PRIVATE_ANSWER"
    forget(db, answer)
    loaded = load_question(db, question_id)
    assert loaded is not None and loaded.answer == ""
    assert "PRIVATE_ANSWER" not in str(list_open_questions(db, thread.id))


@pytest.mark.parametrize("reference", ["trigger", "nested_source"])
def test_inbox_claim_render_and_cascade_follow_independent_event_source(db_session, reference):
    from aiive.forget.content_scope import load_scope
    from aiive.forget.content_scrub import scrub_raw_history
    db = db_session
    thread, task, _ = goal(db)
    turn = _running_turn(db, thread)
    hidden = signal(db, thread.id)
    visible = signal(db, thread.id, "VISIBLE_EVENT")

    def enqueue(event, marker, priority):
        payload = {"task_id": task.id, "reason": marker, "trigger_event_id": event.id,
            "event_type": event.event_type, "event_payload": dict(event.payload)}
        if reference == "nested_source":
            payload.pop("trigger_event_id")
            payload["event_payload"]["source_event_id"] = event.id
        return enqueue_turn_event(db, thread_id=thread.id, event_type="goal_trigger_fired", source="goal_trigger",
            dedupe_key=uid(), priority=priority, payload=payload)

    cached = enqueue(hidden, "PRIVATE_CACHED", 90)
    pending = enqueue(hidden, "PRIVATE_PENDING", 80)
    safe = enqueue(visible, "VISIBLE_INBOX", 50)
    db.commit()
    assert claim_pending_events(db, thread_id=thread.id, turn_record_id=turn.id, turn_id=turn.turn_id, limit=1) == [cached]
    assert "PRIVATE_CACHED" in build_internal_context(db, [cached])
    operation_id = forget(db, hidden)
    assert TaskReadRepository(db).get_task(task.id) is not None  # E1 remains visible.
    claimed = claim_pending_events(db, thread_id=thread.id, turn_record_id=turn.id, turn_id=turn.turn_id, limit=1)
    assert claimed == [safe]  # Hidden high-priority rows must not starve valid signals.
    text = build_internal_context(db, [cached, pending, safe])
    assert "PRIVATE_" not in text and "VISIBLE_INBOX" in text
    assert build_internal_context(db, [cached]) == ""
    scope = load_scope(db, operation_id, "history_only")
    assert {cached.id, pending.id} <= scope.turn_event_inbox_ids
    assert safe.id not in scope.turn_event_inbox_ids
    scrub_raw_history(db, scope)
    db.expire_all()
    assert db.get(TurnEventInbox, cached.id).payload == {}
    assert db.get(TurnEventInbox, pending.id).payload == {}
    assert db.get(TurnEventInbox, safe.id).payload["reason"] == "VISIBLE_INBOX"


def test_domain_task_event_inherits_real_sources_without_fictitious_chat_ref(db_session):
    db = db_session
    thread, task, _ = goal(db, event_type="task_completed")
    _running_turn(db, thread)
    independent = signal(db, thread.id)
    producer = TaskRepository(db).create_task(thread_id=thread.id, goal="PRIVATE_DOMAIN_TASK", enqueue=False)
    persist_sources(db, "agent_task", producer.id, {"source_event_id": independent.id})
    domain_id = f"{producer.id}:succeeded"
    assert db.get(Event, domain_id) is None
    assert GoalTriggerService(db).dispatch_event(GoalEvent(event_id=domain_id, event_type="task_completed",
        source="task", thread_id=thread.id, payload={"task_id": producer.id, "title": "PRIVATE_DOMAIN_TASK"})) == 1
    inbox = db.query(TurnEventInbox).one()
    assert "PRIVATE_DOMAIN_TASK" in build_internal_context(db, [inbox])
    assert not db.query(ContentProvenanceRef).filter_by(source_type="event", source_id=domain_id).count()
    assert db.query(ContentProvenanceRef).filter_by(owner_type="turn_event_inbox", owner_id=inbox.id,
        source_type="event", source_id=independent.id).count()
    forget(db, independent)
    assert TaskReadRepository(db).get_task(task.id) is not None
    assert build_internal_context(db, [inbox]) == ""


def test_memory_only_scrubs_derived_inbox_but_retains_original_chat(db_session):
    from aiive.forget.content_scope import load_scope
    from aiive.forget.content_scrub import scrub_raw_history
    from tests.integration.backend.test_forget_v6261 import memory

    db = db_session
    thread, task, original = goal(db)
    _running_turn(db, thread)
    record = memory(db, original)
    inbox = enqueue_turn_event(db, thread_id=thread.id, event_type="goal_trigger_fired", source="goal_trigger",
        dedupe_key=uid(), payload={"task_id": task.id, "memory_id": record.id, "reason": "PRIVATE_MEMORY"})
    db.commit()
    assert "PRIVATE_MEMORY" in build_internal_context(db, [inbox])
    result = execute_phase_a_shield(db, mode="memory_only", memory_ids=[record.id], requested_by=uid())
    db.commit()
    assert build_internal_context(db, [inbox]) == ""
    scope = load_scope(db, result["operation_id"], "memory_only")
    assert inbox.id in scope.turn_event_inbox_ids
    scrub_raw_history(db, scope)
    db.expire_all()
    assert db.get(TurnEventInbox, inbox.id).payload == {}
    assert db.get(Event, original.id).payload


def test_domain_knowledge_event_follows_underlying_job_event_sources(db_session):
    from aiive.forget.content_scope import load_scope
    from aiive.forget.content_scrub import scrub_raw_history

    db = db_session
    thread, _, _ = goal(db, event_type="knowledge_maintenance_completed")
    _running_turn(db, thread)
    original = signal(db, thread.id)
    operation_id = uid()
    db.add(OutboxJob(operation_id=operation_id, job_type="knowledge_maintenance", status="completed",
        payload={"source_event_ids": [original.id]}))
    db.flush()
    domain_id = f"{operation_id}:completed"
    assert GoalTriggerService(db).dispatch_event(GoalEvent(event_id=domain_id,
        event_type="knowledge_maintenance_completed", source="knowledge", thread_id=thread.id,
        payload={"operation_id": operation_id, "summary": "PRIVATE_DOMAIN_RESULT"})) == 1
    inbox = db.query(TurnEventInbox).one()
    assert "PRIVATE_DOMAIN_RESULT" in build_internal_context(db, [inbox])
    assert not db.query(ContentProvenanceRef).filter_by(source_type="event", source_id=domain_id).count()
    operation = forget(db, original)
    assert build_internal_context(db, [inbox]) == ""
    scope = load_scope(db, operation, "history_only")
    assert inbox.id in scope.turn_event_inbox_ids
    scrub_raw_history(db, scope)
    db.expire_all()
    assert db.get(TurnEventInbox, inbox.id).payload == {}


def test_forgotten_chat_event_cannot_fire_but_new_event_still_fires_once(db_session):
    db = db_session
    thread, task, _ = goal(db, event_type="runtime_signal")
    hidden = signal(db, thread.id)
    forget(db, hidden)
    assert GoalTriggerService(db).scan() == 0
    plan = GoalService(db).load_plan(task)
    assert plan.step("next").trigger_count == 0
    assert plan.step("next").last_trigger_key == ""
    assert db.query(OutboxJob).filter_by(job_type="goal_trigger_decision").count() == 0
    safe = signal(db, thread.id, "VISIBLE_EVENT")
    assert GoalTriggerService(db).scan() == 1
    assert GoalTriggerService(db).scan() == 0
    job = db.query(OutboxJob).filter_by(job_type="goal_trigger_decision").one()
    assert job.payload["trigger_event_id"] == safe.id


def test_event_forgotten_after_scan_read_does_not_publish_trigger(db_session, monkeypatch):
    from aiive.task_runtime import goal_trigger_service as module

    db = db_session
    thread, task, _ = goal(db, event_type="runtime_signal")
    event = signal(db, thread.id)
    evaluate = module.evaluate_rule

    def evaluate_then_forget(*args, **kwargs):
        firing = evaluate(*args, **kwargs)
        assert firing is not None
        forget(db, event)
        return firing

    monkeypatch.setattr(module, "evaluate_rule", evaluate_then_forget)
    assert GoalTriggerService(db).scan() == 0
    assert db.query(OutboxJob).filter_by(job_type="goal_trigger_decision").count() == 0
    assert GoalService(db).load_plan(task).step("next").last_trigger_key == ""


def test_forgotten_step_event_cannot_fire_and_valid_step_event_reaches_worker(db_session, monkeypatch):
    from tests.unit.backend.test_goal_triggers import _patch_llm
    from tests.support import ensure_running_claim
    from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome
    from aiive.worker.handlers_goal_triggers import handle_goal_trigger_decision

    db = db_session
    thread, task, _ = goal(db, event_type="goal_step_completed")
    hidden = signal(db, thread.id)
    TaskRepository(db).append_event(task.id, "goal_step_completed", {"step_id": "old", "source_event_id": hidden.id})
    forget(db, hidden)
    assert GoalTriggerService(db).scan() == 0
    assert db.query(OutboxJob).filter_by(job_type="goal_trigger_decision").count() == 0
    visible = signal(db, thread.id, "VISIBLE_STEP")
    step_event = TaskRepository(db).append_event(task.id, "goal_step_completed", {"step_id": "old", "source_event_id": visible.id})
    assert GoalTriggerService(db).scan() == 1
    assert GoalTriggerService(db).scan() == 0
    job = db.query(OutboxJob).filter_by(job_type="goal_trigger_decision").one()
    assert job.payload["trigger_key"] == f"event_condition:step-done:{step_event.id}"
    assert job.payload["trigger_event_id"] is None
    fake = _patch_llm(monkeypatch, {"action": "no_action", "reason": "normal"})
    claim = ClaimedJob(job.id, job.job_type, dict(job.payload), job.trace_id, 0, 3, "claim", 1, "worker")
    ensure_running_claim(claim, db=db)
    result = handle_goal_trigger_decision(claim)
    assert result.outcome == HandlerOutcome.COMPLETED
    assert len(fake.calls) == 1
