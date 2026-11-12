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
