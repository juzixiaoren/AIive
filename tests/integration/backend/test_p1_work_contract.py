"""P1 public work contract: safe projections, editable plans and persisted chat cards."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiive.api.routes_agent_tasks import router
from aiive.db.base import get_db
from aiive.db.models import AgentTask, AgentTaskEvent, Event, TurnRecord
from aiive.runtime.thread_state import ThreadState
from aiive.runtime.tool_executor import ToolCallRecord, build_action_cards
from aiive.task_runtime.work_read_model import WorkReadModel
from tests.integration.backend.test_work_inbox_api import seed
from tests.unit.backend.test_goal_question_state_consistency import _ask, _goal, _thread


def client_for(db):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_real_goal_lifecycle_events_project_without_payload(db_session):
    thread = _thread(db_session)
    task_id = _goal(db_session, thread, [{"id": "s", "title": "step", "kind": "autonomous"}])
    client = client_for(db_session)
    question = _ask(db_session, task_id, "s", "PRIVATE_QUESTION")["question_id"]
    view = client.get(f"/api/agent-tasks/{task_id}/work-view").json()
    item = next(i for i in view["attention_items"] if i["kind"] == "goal_question")
    assert item["allowed_actions"] == ["answer", "modify_plan", "reject"]
    assert client.post(f"/api/agent-tasks/{task_id}/goal/questions/answer", json={
        "step_id": "s", "question_id": question, "answer": "PRIVATE_ANSWER", "intent": "reject",
    }).status_code == 200
    model = WorkReadModel(db_session)
    task = db_session.get(AgentTask, task_id)
    timeline = model.timeline(task_id)["items"]
    assert {"goal_confirmed", "goal_step_answered"} <= {e["event_type"] for e in timeline}
    # Internal question bookkeeping stays internal, including after resolution.
    assert not {"goal_question_asked", "goal_question_resolved"} & {e["event_type"] for e in timeline}
    assert all(e["summary"] for e in timeline)
    assert "PRIVATE" not in str(timeline)
    assert timeline == model.work_view(task)["timeline"]


@pytest.mark.parametrize("intent", ["modify", "modify_plan"])
def test_safe_editor_preserves_hidden_trigger_fields(db_session, intent):
    thread = _thread(db_session)
    task_id = _goal(db_session, thread, [{"id": "s", "title": "old", "kind": "autonomous", "trigger": {
        "kind": "event_condition", "event_types": ["user_message"], "match": {"private_filter": "PRESERVE_ME"},
    }}])
    client = client_for(db_session)
    question = _ask(db_session, task_id, "s", "question")["question_id"]
    safe = client.get(f"/api/agent-tasks/{task_id}/work-view").json()
    assert "PRESERVE_ME" not in str(safe)
    changes = {"summary": "edited", "steps": [{"id": "s", "title": "new"}]}
    if intent == "modify":
        response = client.post(f"/api/agent-tasks/{task_id}/goal/modify", json=changes)
    else:
        response = client.post(f"/api/agent-tasks/{task_id}/goal/questions/answer", json={
            "step_id": "s", "question_id": question, "answer": "update", "intent": intent, "plan_changes": changes,
        })
    assert response.status_code == 200, response.text
    task = db_session.get(AgentTask, task_id)
    step = task.task_state["goal"]["steps"][0]
    assert step["title"] == "new" and step["trigger"]["match"] == {"private_filter": "PRESERVE_ME"}


def test_editor_forget_visibility_cannot_be_bypassed(db_session):
    from tests.integration.backend.test_forget_goal_read_bypasses import forget, goal
    _, task, original = goal(db_session)
    task_id = task.id
    forget(db_session, original)
    client = client_for(db_session)
    assert client.post(f"/api/agent-tasks/{task_id}/goal/modify", json={"steps": [{"id": "next", "title": "new"}]}).status_code == 404
    assert client.get(f"/api/agent-tasks/{task_id}/work-view").status_code == 404


@pytest.mark.parametrize("kind", [
    "approval_granted", "approval_denied", "approval_expired", "action_invalidated",
    "goal_modified", "goal_rejected", "goal_ended", "goal_completed", "goal_blocked",
    "goal_step_triggered", "goal_step_waiting_user", "goal_step_due", "goal_step_missed",
])
def test_public_timeline_whitelist_uses_safe_summaries_and_raw_cursor(db_session, kind):
    model = seed(db_session, 1)
    for seq, event_type, visibility in [(1, kind, "task"), (2, kind, "internal"), (3, "unknown_internal_type", "task")]:
        db_session.add(AgentTaskEvent(task_id="00000", sequence=seq, event_type=event_type, visibility=visibility,
                                     payload={"arguments": {"password": "SECRET"}, "summary": "SECRET"}))
    db_session.flush()
    page = model.timeline("00000", 0, 3)
    assert len(page["items"]) == 1 and page["items"][0]["summary"]
    assert page["next_sequence"] == 3 and "SECRET" not in str(page)


def test_delegation_card_persists_through_thread_history_once(db_session):
    state = ThreadState(db_session)
    thread = state.get_or_create_thread()
    db_session.add(TurnRecord(thread_id=thread.id, turn_id="turn", turn_sequence=1, status="completed", request_fingerprint="fp"))
    db_session.flush()
    result = {"ok": True, "result": {"task_id": "work", "title": "Work", "status": "queued", "summary": "Delegated"}}
    cards = [card.model_dump(mode="json") for card in build_action_cards([ToolCallRecord(name="delegate_task", params={}, result=result, status="completed", trace_id="trace")])]
    for index, (kind, payload) in enumerate([
        ("user_message", {"content": "delegate"}),
        ("tool_call", {"name": "delegate_task", "tool_call_id": "call", "params": {}}),
        ("tool_result", {"name": "delegate_task", "tool_call_id": "call", "status": "completed", "result": result}),
        ("llm_response", {"content": "Delegated", "action_cards": cards}),
    ]):
        db_session.add(Event(thread_id=thread.id, turn_id="turn", turn_event_index=index, trace_id="trace", event_type=kind, payload=payload))
    db_session.flush()
    for _ in range(2):
        restored = state.list_thread_messages_page(thread.id)["messages"]
        assert [c for msg in restored for c in msg["action_cards"]] == cards


def test_cors_exposes_download_integrity_headers():
    from aiive.main import app
    middleware = next(m for m in app.user_middleware if m.cls.__name__ == "CORSMiddleware")
    assert set(middleware.kwargs["expose_headers"]) >= {
        "Content-Disposition", "X-AIive-Content-Hash", "X-AIive-Truncated", "X-AIive-Next-Offset",
    }


def test_continue_work_reuses_only_selected_visible_sources(db, tmp_path, monkeypatch):
    from uuid import uuid4

    from aiive.context.run_context import RunContext
    from aiive.control.scope import TaskScope
    from aiive.db.models import OutboxJob, TaskArtifact, TaskEvidence
    from aiive.forget.durable_sources import persist_sources
    from aiive.forget.phase_a_shield import execute_phase_a_shield
    from aiive.storage.object_store import put_content_addressed
    from aiive.task_runtime.agent_executor import AgentDecision
    from aiive.task_runtime.context_assembler import TaskContextAssembler
    from aiive.task_runtime.repository import TaskRepository
    from aiive.task_runtime.runtime import PersistentTaskRuntime
    from aiive.task_runtime.schemas import ActionProposal
    from aiive.tools.registry import get_tool_registry
    from aiive.tools.task_file_tools import ReadTaskArtifactTool, ReadTaskEvidenceTool
    from tests.support import isolate_object_store

    isolate_object_store(monkeypatch, tmp_path)
    thread_id = _thread(db)
    parent = TaskRepository(db).create_task(
        thread_id=thread_id, goal="Write the weekly report", enqueue=False,
        task_brief={"scope": {"allow_network": True, "allow_secrets": True}},
    )
    parent.status = "succeeded"
    parent.report = {"summary": "Previous report"}
    source = Event(thread_id=thread_id, trace_id="source", event_type="user_message", payload={"content": "Risk data"})
    db.add(source)
    payload = b"# Risks\nCurrent status"
    ref, digest = put_content_addressed("fixture", payload)
    artifacts = [
        TaskArtifact(
            task_id=parent.id, name=name, object_bucket=ref.bucket, object_key=ref.key,
            content_hash=digest, content_size=len(payload), mime_type="text/markdown",
        ) for name in ("chosen.md", "unselected.md")
    ]
    evidence = TaskEvidence(
        task_id=parent.id, content_hash="fixture", inline_payload={"risk": "coverage"},
        content_size=19,
    )
    db.add_all([*artifacts, evidence])
    db.flush()
    for artifact in artifacts:
        persist_sources(db, "task_artifact", artifact.id, {"source_event_id": source.id})
    persist_sources(db, "task_evidence", evidence.id, {"source_event_id": source.id})
    db.commit()

    client = client_for(db)
    endpoint = f"/api/agent-tasks/{parent.id}/continuations"
    request = {"request_id": str(uuid4()), "instruction": "Update only the risk section",
               "artifact_ids": [artifacts[0].id], "evidence_ids": [evidence.id]}
    result = client.post(endpoint, json=request)
    assert result.status_code == 200, result.text
    response = result.json()
    child_id = response["task_id"]
    assert client.post(endpoint, json=request).json() == response
    assert client.post(endpoint, json={**request, "instruction": "Change everything"}).status_code == 409
    assert client.post(endpoint, json={**request, "request_id": str(uuid4()), "artifact_ids": ["unavailable"]}).status_code == 404
    assert db.query(OutboxJob).filter(OutboxJob.job_type == "agent_task_run").count() == 1

    child = db.get(AgentTask, child_id)
    scope = TaskScope.from_brief(child.task_brief)
    assert not scope.allow_network and not scope.allow_secrets and not scope.allows_capability("run_shell")
    assert child.goal == request["instruction"] and parent.report["summary"] == "Previous report"
    assert client.get(f"/api/agent-tasks/{child_id}/work-view").json()["continuation"]["source_task_id"] == parent.id
    assembler = TaskContextAssembler(db)
    snapshot = assembler.snapshot(child, [], reporting=True)
    assert snapshot["continuation"]["report"] == "Previous report"
    assert [item["id"] for item in snapshot["continuation"]["artifacts"]] == [artifacts[0].id]
    ctx = RunContext(thread_id=thread_id, trace_id="revision", task_id=child.id)
    registry = get_tool_registry()
    artifact_tool = registry.get("read_task_artifact")
    evidence_tool = registry.get("read_task_evidence")
    assert isinstance(artifact_tool, ReadTaskArtifactTool) and isinstance(evidence_tool, ReadTaskEvidenceTool)
    assert artifact_tool._run(db, ctx, artifacts[0].id)["content"] == payload.decode()
    assert artifact_tool._run(db, ctx, artifacts[1].id)["error_type"] == "task_file_unavailable"
    assert "coverage" in evidence_tool._run(db, ctx, evidence.id)["content"]

    class Revise:
        def decide(self, task, _capabilities):
            return AgentDecision(
                proposal=ActionProposal(decision="finish", summary="Updated risks", report_status="succeeded"),
                model="fixture", token_usage={}, input_snapshot={}, prompt_refs=[],
            )

        def generate_report(self, task, _outcome, **_kwargs):
            assert assembler.snapshot(task, [], reporting=True)["continuation"]["report"] == "Previous report"
            return "Revised risk section", {}, False

    assert PersistentTaskRuntime(db, agent_executor=Revise()).run(child_id)["status"] == "succeeded"
    view = client.get(f"/api/agent-tasks/{child_id}/work-view").json()
    assert view["task"]["report"]["summary"] == "Revised risk section"

    execute_phase_a_shield(db, mode="history_only", event_ids=[source.id], requested_by="owner")
    db.commit()
    assert client.get(f"/api/agent-tasks/{parent.id}/work-view").status_code == 200
    assert client.get(f"/api/agent-tasks/{child_id}/work-view").status_code == 404
    assert client.post(endpoint, json=request).status_code == 404
