"""Experience through the real finalize, Outbox, context and review entry points."""
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aiive.api.routes_experience import router
from aiive.core.action_planner import MemorySignalDecision
from aiive.db.base import get_db
from aiive.db.models import Base, Event, ExperienceEpisode, OutboxJob, Thread, TurnRecord
from aiive.experience.capture import JOB_CAPTURE, build_episode, discovery_classification, visible_signals
from aiive.experience.effects import ExperienceEffectEvaluator
from aiive.experience.families import stable_id
from aiive.experience.feedback import analyze_follow_ups, classify_follow_up, record_feedback
from aiive.experience.reflector import Reflector
from aiive.runtime.agent_graph import AgentGraphResult, TurnCancelled
from aiive.runtime.context_assembler import ContextAssembler
from aiive.runtime.token_models import ModelProfile
from aiive.runtime.token_counter import LiteLLMTokenCounter
from aiive.runtime.turn_execution import TurnExecutionService
from aiive.runtime.working_state import WorkingStateService
from aiive.memory.extraction_policy import MessageSource
from aiive.skills.catalog import SkillService
from aiive.task_runtime.agent_executor import AgentDecision, TaskAgentExecutor
from langchain_core.messages import AIMessage
from aiive.task_runtime.context_assembler import TaskContextAssembler
from aiive.task_runtime.repository import TaskRepository
from aiive.task_runtime.runtime import PersistentTaskRuntime
from aiive.task_runtime.schemas import ActionProposal
from aiive.tool_discovery.service import DiscoveryService
from aiive.tools.registry import get_tool_registry
from aiive.worker.handler_registry import HandlerRegistry
from aiive.worker.outbox_heartbeat import ActiveClaimRegistry
from aiive.worker.outbox_handlers import register_all
from aiive.worker.outbox_worker import OutboxWorker


def _thread(db):
    thread = Thread(title="Experience test")
    db.add(thread)
    db.flush()
    return thread


def _turn(db, thread, seq, text, *, at=None):
    at = at or datetime.now(UTC)
    event = Event(thread_id=thread.id, trace_id="", turn_id=f"{thread.id}-{seq}", event_type="user_message",
                  turn_event_index=0, payload={"content": text}, created_at=at)
    db.add(event)
    db.flush()
    turn = TurnRecord(thread_id=thread.id, turn_id=event.turn_id, turn_sequence=seq,
                      status="running", execution_id="owned", request_event_id=event.id, created_at=at)
    db.add(turn)
    db.commit()
    return turn


def _finalize(turn):
    service = TurnExecutionService.__new__(TurnExecutionService)
    service._profile = ModelProfile.from_config("deepseek", "deepseek-chat")
    service._ws_service = WorkingStateService()
    service._message_source = MessageSource.USER
    service._finalize_turn(
        turn, "owned", AgentGraphResult(
            reply="This is the response", trace_id=turn.id,
            memory_signal=MemorySignalDecision(action="skip", confidence=1.0, reason="fixture"),
        ),
        SimpleNamespace(lease_lost=False),
    )


def _poll():
    registry = HandlerRegistry()
    register_all(registry)
    return OutboxWorker("test", registry, ActiveClaimRegistry()).poll(only_types=frozenset({JOB_CAPTURE}))


class _Finish:
    def __init__(self, status="succeeded"):
        self.status = status

    def decide(self, task, capabilities):
        return AgentDecision(
            proposal=ActionProposal(decision="finish", summary="Done", report_status=self.status),
            model="fixture", token_usage={"prompt_tokens": 20, "completion_tokens": 5},
            input_snapshot={}, prompt_refs=[],
        )


@pytest.fixture
def experience_db(tmp_path, monkeypatch):
    from aiive.runtime import turn_execution
    from aiive.worker import handlers_experience, outbox_worker

    engine = create_engine(f"sqlite:///{tmp_path / 'experience.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    # A lazy handler import would retain a temporary base.SessionLocal after teardown.
    for module in (turn_execution, handlers_experience, outbox_worker):
        monkeypatch.setattr(module, "SessionLocal", factory)
    with factory() as db:
        db.info["session_factory"] = factory
        yield db
    engine.dispose()


@pytest.fixture
def api(experience_db):
    factory = experience_db.info["session_factory"]

    def session():
        with factory() as db:
            yield db

    app = FastAPI()
    app.dependency_overrides[get_db] = session
    app.include_router(router)
    with TestClient(app) as client:
        yield client


def test_atomic_turn_and_task_capture_and_cancel(experience_db, api):
    db = experience_db
    thread = _thread(db)
    turn = _turn(db, thread, 1, "Summarize a PDF document")
    task = TaskRepository(db).create_task(
        thread_id=thread.id, source_turn_record_id=turn.id, task_type="document",
        goal="Summarize the PDF", enqueue=False,
    )
    db.commit()
    assert PersistentTaskRuntime(db, agent_executor=_Finish()).run(task.id)["status"] == "succeeded"
    assert _poll() == 1  # Task may finish before its parent Turn exists.
    child = db.query(ExperienceEpisode).filter_by(kind="task").one()
    assert child.parent_id == stable_id("turn", turn.id)
    assert child.outcome == "agent_reported_success"
    assert child.metrics["tokens"] == 25
    assert api.get("/api/experience").json()["episodes"][0]["title"] == task.title
    detail = api.get(f"/api/experience/episodes/{child.id}").json()
    assert detail["result_preview"] == task.report["summary"]

    _finalize(turn)
    assert _poll() == 1
    parent = db.get(ExperienceEpisode, child.parent_id)
    assert parent is not None and parent.outcome == "unknown"
    assert parent.refs["context_snapshot_id"]
    retry = db.query(OutboxJob).filter_by(job_type=JOB_CAPTURE).all()
    for job in retry:
        assert build_episode(db, job.payload)
    assert db.query(ExperienceEpisode).count() == 2

    cancelled = _turn(db, thread, 2, "Cancelled message")
    cancelled.cancel_requested_at = datetime.now(UTC)
    db.commit()
    with pytest.raises(TurnCancelled):
        _finalize(cancelled)
    assert db.query(OutboxJob).filter_by(job_type=JOB_CAPTURE).count() == 2
    assert build_episode(db, {"turn_record_id": cancelled.id}) is None


def test_follow_up_targets_immediate_visible_episode(experience_db, api):
    db = experience_db
    thread = _thread(db)
    first = _turn(db, thread, 1, "请总结 PDF 文档")
    _finalize(first)
    second = _turn(db, thread, 2, "不对，我要总结附录")
    _finalize(second)
    third = _turn(db, thread, 3, "另一个问题：如何整理知识库？")
    _finalize(third)
    assert _poll() == 3
    now = datetime.now(UTC)
    assert analyze_follow_ups(db, now) == 2
    db.commit()
    assert analyze_follow_ups(db, now) == 0
    episodes = {row["source_id"]: row for row in api.get("/api/experience").json()["episodes"]}
    assert episodes[first.id]["outcome"] == "corrected"
    assert episodes[second.id]["outcome"] == "unknown"
    assert episodes[third.id]["outcome"] == "unknown"
    detail = api.get(f"/api/experience/episodes/{episodes[second.id]['id']}").json()
    assert any(signal["kind"] == "new_request" for signal in detail["signals"])
    assert classify_follow_up("请重新生成摘要", "请总结") == "rework"
    assert classify_follow_up("继续展开", "请总结") == "continuation"


def test_follow_up_scan_advances_past_ineligible_page(experience_db, monkeypatch):
    from aiive.experience import feedback

    monkeypatch.setattr(feedback, "_BATCH_SIZE", 1)
    db = experience_db
    now = datetime.now(UTC)
    for _ in range(5):
        thread = _thread(db)
        old = _turn(db, thread, 1, "Summarize the PDF", at=now - timedelta(days=5))
        _finalize(old)
        old.updated_at = now - timedelta(days=5)
        db.commit()
        _turn(db, thread, 2, "不对", at=now - timedelta(days=2))

    thread = _thread(db)
    eligible = _turn(db, thread, 1, "Summarize the PDF")
    _finalize(eligible)
    db.refresh(eligible)
    at = eligible.updated_at.replace(tzinfo=UTC) + timedelta(seconds=1)
    _turn(db, thread, 2, "不对，我要总结附录", at=at)
    assert _poll() == 6

    assert analyze_follow_ups(db, at) == 1
    db.commit()
    signals = visible_signals(db, stable_id("turn", eligible.id))
    assert [row.kind for row in signals if row.details.get("method") == "follow_up"] == ["corrected"]
    assert analyze_follow_ups(db, at) == 0


def test_episode_history_cursor_and_feedback_preview(experience_db, api):
    db = experience_db
    stamp = datetime.now(UTC)
    thread = _thread(db)
    titles = {}
    for i in range(3):
        title = f"Summarize document {i}"
        turn = _turn(db, thread, i + 1, title)
        _finalize(turn)
        titles[stable_id("turn", turn.id)] = title
    assert _poll() == 3
    db.query(ExperienceEpisode).update({ExperienceEpisode.completed_at: stamp})
    # Old projections whose sources disappeared must not exhaust the visible page.
    for i in range(18):
        db.add(ExperienceEpisode(id=stable_id("missing", str(i)), kind="turn", source_id=str(i),
                                 thread_id=thread.id, task_type="chat", family="general", outcome="unknown",
                                 completed_at=stamp + timedelta(minutes=1)))
    db.commit()
    ids = sorted(titles)
    home = api.get("/api/experience", params={"limit": 1}).json()
    assert [row["id"] for row in home["episodes"]] == ids[:1]
    assert home["episodes"][0]["title"] == titles[ids[0]]
    assert "result_preview" not in home["episodes"][0]

    # A deleted page anchor must not invalidate the next request.
    db.delete(db.get(ExperienceEpisode, ids[0]))
    db.commit()
    older = api.get("/api/experience/episodes", params={"cursor": home["episodes_next_cursor"], "limit": 2}).json()
    assert [row["id"] for row in older["items"]] == ids[1:]
    assert older["next_cursor"] is None
    assert api.get("/api/experience/episodes", params={"cursor": "bad"}).status_code == 422

    path = f"/api/experience/episodes/{ids[1]}"
    assert api.get(path).json()["result_preview"] == "This is the response"
    episode = db.get(ExperienceEpisode, ids[1])
    reply = db.get(Event, episode.refs["assistant_event_id"])
    reply.payload = {**reply.payload, "content": "答" * 2001}
    db.commit()
    feedback = api.post(f"{path}/feedback", json={"kind": "rework"}).json()
    assert feedback["outcome"] == "rework"
    assert feedback["result_truncated"] and feedback["result_preview"] == "答" * 2000


class _Curator:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        prompt = json.loads(messages[-1]["content"])
        traces = prompt["cohorts"]["document"]
        assert {row["outcome"] for row in traces} >= {"explicit_positive", "corrected"}
        current = next((row for row in prompt["insights"] if row["kind"] == "skill"), None)
        evidence = [{"episode_id": row["episode_id"], "source_id": row["source_ids"][0]} for row in traces]
        op = {
            "op": "EDIT" if current else "ADD",
            "insight_id": current["id"] if current else None,
            "family": "document", "kind": "skill", "title": "Check PDF coverage",
            "conditions": "Use when asked to summarize a PDF document",
            "match_terms": ["summarize", "pdf"], "steps": ["Extract headings", "Compare section coverage"],
            "required_capabilities": [], "expected_result": "Summary with all requested sections",
            "verification": "Confirm each requested section has a matching paragraph",
            "recovery": "Ask which sections to include when ambiguous",
            "root_cause": "Unclear section coverage", "evidence": evidence,
        }
        if current:
            op["steps"].append("Check the appendix")
        forged = {**op, "evidence": [*evidence, {"episode_id": "fake", "source_id": "unknown"}]}
        return SimpleNamespace(content=json.dumps({"operations": [forged, op]}))


def test_reflector_selects_independent_positive_examples(experience_db):
    db = experience_db
    independent = _turn(db, _thread(db), 1, "Summarize a PDF document")
    _finalize(independent)
    independent_id = stable_id("turn", independent.id)
    thread = _thread(db)
    parent = _turn(db, thread, 1, "Summarize a separate PDF document")
    _finalize(parent)
    for _ in range(5):
        task = TaskRepository(db).create_task(
            thread_id=thread.id, source_turn_record_id=parent.id, task_type="document",
            goal="Summarize a PDF document", enqueue=False,
        )
        db.commit()
        assert PersistentTaskRuntime(db, agent_executor=_Finish()).run(task.id)["status"] == "succeeded"
    assert _poll() == 7
    for episode in db.query(ExperienceEpisode).filter(
        (ExperienceEpisode.id == independent_id) | (ExperienceEpisode.parent_id == stable_id("turn", parent.id)),
    ):
        record_feedback(db, episode, "explicit_positive")
    db.commit()

    class InspectBatch:
        def chat(self, messages, **kwargs):
            group = json.loads(messages[-1]["content"])["cohorts"]["document"]
            positives = [row["independent_key"] for row in group if row["outcome"] == "explicit_positive"]
            assert len(positives) == 2
            assert set(positives) == {independent_id, stable_id("turn", parent.id)}
            return SimpleNamespace(content='{"operations": []}')

    assert Reflector(InspectBatch(), session_factory=db.info["session_factory"]).run()["llm_called"]


def test_curate_review_inject_and_track_versions(experience_db, api):
    db = experience_db
    for positive in (True, True, False):
        turn = _turn(db, _thread(db), 1, "Summarize the PDF document")
        _finalize(turn)
        _poll()
        episode_id = stable_id("turn", turn.id)
        kind = "explicit_positive" if positive else "explicit_negative"
        assert api.post(f"/api/experience/episodes/{episode_id}/feedback", json={"kind": kind}).status_code == 200

    model = _Curator()
    reflector = Reflector(model, session_factory=db.info["session_factory"])
    first = reflector.run()
    assert model.calls == 1 and len(first["proposals"]) == 1
    assert api.post(
        f"/api/experience/episodes/{episode_id}/feedback", json={"kind": "explicit_negative"},
    ).status_code == 200
    assert not reflector.run()["llm_called"]
    insight_id = first["proposals"][0]
    prefix = f"/api/experience/insights/{insight_id}"
    detail = api.get(prefix).json()
    assert (detail["support_count"], detail["refute_count"]) == (2, 1)
    assert detail["effect"]["status"] == "insufficient_data"
    assert api.post(f"{prefix}/accept").json()["status"] == "active"
    assert api.post(f"{prefix}/accept").status_code == 409

    thread = _thread(db)
    profile = ModelProfile.from_config("deepseek", "deepseek-chat")
    context = ContextAssembler(LiteLLMTokenCounter(profile=profile)).assemble(db, "Summarize this PDF document", thread)
    assert any("Check PDF coverage" in message["content"] for message in context.messages)
    uses = context.snapshot.selection_trace["experience_uses"]
    assert uses == [{"id": detail["insight_id"], "version": 1, "kind": "skill"}]
    assert SkillService(db).get(detail["insight_id"]).version == "1"

    task = TaskRepository(db).create_task(
        thread_id=thread.id, task_type="document", goal="Summarize a PDF document", enqueue=False,
    )
    db.commit()
    messages, snapshot, _ = TaskContextAssembler(db).messages(task, [])
    assert snapshot["experience"][0]["version"] == 1
    assert "Check PDF coverage" in messages[0]["content"]
    db.commit()
    assert api.post(f"{prefix}/disable").json()["status"] == "disabled"
    assert not TaskContextAssembler(db).snapshot(task, [])["experience"]
    db.commit()
    assert api.post(f"{prefix}/enable").json()["status"] == "active"

    # Execute a real Worker decision with the selected version and capture its observed result.
    class FinishModel:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, messages, **kwargs):
            assert "Check PDF coverage" in messages[0].content
            return AIMessage(content="", tool_calls=[
                {"name": "task_finish", "args": {"summary": "PDF summary complete"}, "id": "finish-call"},
            ])

    executor = TaskAgentExecutor(db, chat_model=FinishModel())
    def report(*args, **kwargs):
        return "PDF summary complete", {}, False

    executor.generate_report = report
    assert PersistentTaskRuntime(db, agent_executor=executor).run(task.id)["status"] == "succeeded"
    assert _poll() == 1
    new_episode = db.get(ExperienceEpisode, stable_id("task", task.id))
    assert new_episode.uses == uses
    record_feedback(db, new_episode, "explicit_positive")
    db.commit()
    assert api.get(prefix).json()["usage"] == {"samples": 1, "helpful": 1, "harmful": 0}
    revised = reflector.run()
    assert len(revised["proposals"]) == 1
    second = api.post(f"/api/experience/insights/{revised['proposals'][0]}/accept").json()
    assert second["version"] == 2
    assert api.get(prefix).json()["status"] == "superseded"
    assert db.get(ExperienceEpisode, new_episode.id).uses[0]["version"] == 1


def test_delegated_skill_wins_over_automatic_matches(experience_db, monkeypatch):
    from aiive.context.run_context import RunContext
    from aiive.db.models import AgentTask, ExperienceInsight
    from aiive.task_runtime.agent_executor import _compact_history
    from aiive.task_runtime.tools import build_main_agent_registry

    db = experience_db
    monkeypatch.setattr("aiive.task_runtime.tools.SessionLocal", db.info["session_factory"])
    evidence = []
    for _ in range(2):
        turn = _turn(db, _thread(db), 1, "Summarize PDF tables and appendices")
        _finalize(turn)
        _poll()
        episode = db.get(ExperienceEpisode, stable_id("turn", turn.id))
        feedback = record_feedback(db, episode, "explicit_positive")
        evidence.append({"episode_id": episode.id, "source_id": feedback.source_id, "signal_id": feedback.id,
                         "trace_source_id": turn.id, "role": "support", "outcome": "explicit_positive",
                         "independent_key": episode.id})
    for identity, terms in [("appendix", ["appendices", "tables"]), ("outline", ["summarize", "pdf"]),
                            ("coverage", ["summarize", "document"])]:
        db.add(ExperienceInsight(
            id=identity, insight_id=identity, version=1, status="active", kind="skill", operation="ADD",
            family="document", title=identity, support_count=2, evidence=evidence,
            body={"conditions": identity, "match_terms": terms, "required_capabilities": [],
                  "steps": [identity], "expected_result": identity, "verification": identity, "recovery": identity},
        ))
    db.commit()
    result = build_main_agent_registry().execute(
        "delegate_task", {"goal": "Summarize this PDF document", "skill_id": "appendix"}, "trusted_user_command",
        run_context=RunContext(thread_id=turn.thread_id, trace_id=turn.id),
    )
    assert result["ok"], result
    task = db.get(AgentTask, result["result"]["task_id"])
    assembler = TaskContextAssembler(db)
    messages, snapshot, _ = assembler.messages(task, [])
    assert snapshot["experience"][0]["id"] == "appendix"
    assert len(snapshot["experience"]) == 2
    assert "appendix" in messages[0]["content"]
    assert _compact_history(snapshot)
    assert [item["id"] for item in snapshot["experience"]] == ["appendix"]

    def selected_ids():
        return [item["id"] for item in assembler.snapshot(task, [])["experience"]]

    selected = db.get(ExperienceInsight, "appendix")
    selected.body = {**selected.body, "required_capabilities": ["web_search"]}
    db.commit()
    assert "appendix" not in selected_ids()  # The task has no network authorization.
    selected.body = {**selected.body, "required_capabilities": []}
    selected.status = "disabled"
    db.commit()
    assert "appendix" not in selected_ids()
    selected.status = "active"
    record_feedback(db, episode, "rework")
    db.commit()
    assert selected_ids() == []


def test_tool_hint_changes_discovery_and_respects_scope(experience_db, api):
    db = experience_db
    registry = get_tool_registry()
    capability = "get_runtime_identity"
    alias = "obscurelookup"
    for _ in range(2):
        thread = _thread(db)
        task = TaskRepository(db).create_task(thread_id=thread.id, goal="Discover runtime",
                                              enqueue=False)
        _, run = TaskRepository(db).start_run(task.id, trigger="test")
        repo = TaskRepository(db)
        repo.append_event(task.id, "tool_search", {
            "query": alias, "matched": [], "status": "disabled", "experience_classification": "search_no_results",
        }, run_id=run.id)
        repo.append_event(task.id, "tool_describe", {"capability_id": capability, "found": True, "in_scope": True}, run_id=run.id)
        # The model's actual recovery is required in addition to the Describe.
        from aiive.db.models import AgentAction

        action = AgentAction(task_id=task.id, run_id=run.id, sequence=1, capability_id=capability,
                             arguments_hash="hash", descriptor_hash="desc", idempotency_key=task.id, status="succeeded")
        db.add(action)
        db.flush()
        from aiive.task_runtime.read_repository import inherit_sources

        inherit_sources(db, "agent_action", action.id, [("agent_task", task.id)])
        PersistentTaskRuntime(db)._complete(task, run, status="succeeded", summary="Found")
    _poll()
    reflected = Reflector(None, session_factory=db.info["session_factory"]).run()
    assert not reflected["llm_called"] and len(reflected["proposals"]) == 1
    revision = reflected["proposals"][0]
    assert api.post(f"/api/experience/insights/{revision}/accept").status_code == 200

    from aiive.experience.retrieval import ExperienceInsightRetriever

    service = DiscoveryService()
    hints = ExperienceInsightRetriever(db).tool_hints("general", alias, registry)
    assert service.search(registry, alias)["tools"] == []
    result = service.search(registry, alias, experience_hints=hints)
    assert [item["capability_id"] for item in result["tools"]] == [capability]
    assert result["experience_uses"][0]["version"] == 1
    assert service.search(registry.scoped(set()), alias, experience_hints=hints)["tools"] == []
    assert discovery_classification(db, "tool_search", {"matched": [], "status": "embedding_unavailable"}) == "discovery_temporary_failure"
    assert discovery_classification(db, "tool_describe", {"found": True, "in_scope": False}) == "capability_out_of_scope"


def test_effects_use_correct_denominators(db_session):
    db = db_session
    thread = _thread(db)
    now = datetime.now(UTC)
    rows = []
    for index in range(5):
        row = ExperienceEpisode(
            id=str(index), kind="turn", source_id=str(index), thread_id=thread.id, task_type="chat",
            family="document", outcome="unknown", completed_at=now,
            uses=[{"id": "skill", "version": 1, "kind": "skill"}],
            metrics={"actions": 2, "tokens": None, "duration_ms": 50,
                     "tool_calls": 2, "tool_failures": 1, "searches": 0, "search_misses": 0},
        )
        rows.append(row)
        db.add(row)
    db.flush()
    record_feedback(db, rows[0], "rework")
    db.flush()
    result = ExperienceEffectEvaluator(db).summarize(rows)
    assert result["rates"]["rework"] == {"numerator": 1, "denominator": 5, "value": 0.2, "status": "observed"}
    assert result["rates"]["tool_failure"]["denominator"] == 10
    assert result["rates"]["skill_success"]["denominator"] == 1
    assert result["rates"]["task_failure"]["status"] == "insufficient_data"
    assert result["medians"]["tokens"]["samples"] == 0
    assert result["medians"]["actions"]["value"] == 2



def test_routed_preference_and_mcp_candidate(experience_db, api, monkeypatch):
    db = experience_db
    turn = _turn(db, _thread(db), 1, "以后回答请先给结论")
    _finalize(turn)
    assert _poll() == 1
    capability = "missing_powerpoint_transform"
    for _ in range(2):
        task = TaskRepository(db).create_task(thread_id=_thread(db).id, goal="Transform slides", enqueue=False)
        _, run = TaskRepository(db).start_run(task.id, trigger="test")
        TaskRepository(db).append_event(
            task.id, "tool_describe",
            {"capability_id": capability, "experience_classification": "capability_missing_from_catalog"},
            run_id=run.id,
        )
        PersistentTaskRuntime(db)._complete(task, run, status="failed", summary="Unavailable")
    assert _poll() == 2
    Reflector(None, session_factory=db.info["session_factory"]).run()
    candidates = {row["route"]: row for row in api.get("/api/experience").json()["candidates"]}
    preference = candidates["memory"]
    assert preference["preference"] == "以后回答请先给结论"
    response = api.post(f"/api/experience/candidates/{preference['candidate_id']}/accept", json={})
    assert response.status_code == 200, response.text
    assert response.json()["progress"]["status"] == "written"
    from aiive.db.models import CapabilityPlan, MemoryRecord

    record = db.get(MemoryRecord, response.json()["progress"]["id"])
    assert record is not None and record.canonical_key == "user.preference.response_style"

    def plan(session, goal):
        row = CapabilityPlan(goal=goal, status="evaluated")
        session.add(row)
        session.flush()
        return {"plan_id": row.id}

    monkeypatch.setattr("aiive.tools.builtin_mcp.persist_capability_plan", plan)
    gap = candidates["mcp"]
    assert gap["occurrences"] == 2 and len(gap["experience_refs"]) == 2
    response = api.post(f"/api/experience/candidates/{gap['candidate_id']}/accept", json={})
    assert response.status_code == 200, response.text
    assert response.json()["progress"]["status"] == "evaluated"
    assert db.get(CapabilityPlan, response.json()["progress"]["id"]) is not None

    # The task remains visible when a source belonging only to its discovery event is forgotten.
    from aiive.forget.durable_sources import persist_sources
    from aiive.forget.phase_a_shield import execute_phase_a_shield

    source = Event(thread_id=turn.thread_id, trace_id="discovery-input", event_type="user_message",
                   payload={"content": "Earlier discovery input"})
    db.add(source)
    db.flush()
    ref = gap["experience_refs"][0]
    persist_sources(db, "agent_task_event", ref["source_id"], {"source_event_id": source.id})
    db.commit()
    execute_phase_a_shield(db, mode="history_only", event_ids=[source.id], requested_by="owner")
    db.commit()
    from aiive.task_runtime.read_repository import TaskReadRepository

    episode = db.get(ExperienceEpisode, ref["episode_id"])
    assert TaskReadRepository(db).get_task(episode.source_id) is not None
    episode_path = f"/api/experience/episodes/{episode.id}"
    assert api.get(episode_path).status_code == 404
    assert api.post(f"{episode_path}/feedback", json={"kind": "rework"}).status_code == 404
    overview = api.get("/api/experience").json()
    assert episode.id not in {row["id"] for row in overview["episodes"]}
    assert gap["candidate_id"] not in {row["candidate_id"] for row in overview["candidates"]}
    candidate_path = f"/api/experience/candidates/{gap['candidate_id']}"
    assert api.get(candidate_path).status_code == 404
    assert api.post(f"{candidate_path}/accept", json={}).status_code == 404
    assert api.post(f"{candidate_path}/reject", json={}).status_code == 404




def test_forget_scrubs_experience_text_and_evidence(experience_db, api):
    from aiive.db.models import ExperienceInsight
    from aiive.experience.forget import scrub_experience
    from aiive.forget.content_scope import ContentScope

    db = experience_db
    turns = []
    for positive in (True, True, False):
        turn = _turn(db, _thread(db), 1, "Summarize a PDF document")
        turns.append(turn)
        _finalize(turn)
        _poll()
        episode = db.get(ExperienceEpisode, stable_id("turn", turn.id))
        record_feedback(db, episode, "explicit_positive" if positive else "explicit_negative")
        db.commit()
    model = _Curator()
    revision = Reflector(model, session_factory=db.info["session_factory"]).run()["proposals"][0]
    target = turns[0]
    from aiive.forget.phase_a_shield import execute_phase_a_shield

    prefix = f"/api/experience/insights/{revision}"
    execute_phase_a_shield(db, mode="history_only", event_ids=[target.request_event_id], requested_by="owner")
    db.commit()
    assert api.get(prefix).status_code == 404
    for action in ("reject", "accept", "disable", "enable"):
        assert api.post(f"{prefix}/{action}").status_code == 404
    db.expire_all()
    assert db.get(ExperienceInsight, revision).status == "pending"
    scope = ContentScope("test-forget", "everywhere", "thread", datetime.now(UTC),
                         turn_tokens={target.id, target.turn_id}, event_ids={target.request_event_id})
    counts = scrub_experience(db, scope)
    db.commit()
    assert counts["experience_episodes"] == counts["experience_insights"] == 1
    assert api.get(f"/api/experience/episodes/{stable_id('turn', target.id)}").status_code == 404
    assert api.get(f"/api/experience/insights/{revision}").status_code == 404
    row = db.get(ExperienceInsight, revision)
    assert row.status == "redacted" and row.body == {} and row.evidence == []
