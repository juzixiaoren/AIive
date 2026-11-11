from datetime import UTC, datetime

import pytest
from aiive.db.models import AgentTask, AgentTaskEvent, ConversationState, Thread
from aiive.task_runtime.work_read_model import WorkReadModel


def seed(db, count=3):
    db.add(Thread(id="thread", title="work"))
    db.flush()
    db.add(ConversationState(id="default", current_thread_id="thread"))
    now = datetime.now(UTC)
    for i in range(count):
        db.add(
            AgentTask(
                id=f"{i:05d}",
                thread_id="thread",
                title="work",
                goal="work",
                status="queued",
                updated_at=now,
            )
        )
    db.flush()
    return WorkReadModel(db)


def test_keyset_equal_timestamps_and_read_only(db_session):
    model = seed(db_session)
    first = model.inbox("active", 1)
    second = model.inbox("active", 1, first["next_cursor"])
    assert first["items"][0]["task_id"] == "00002"
    assert second["items"][0]["task_id"] == "00001"
    for bucket in ["waiting", "attention"]:
        with pytest.raises(ValueError):
            model.inbox(bucket, 1, first["next_cursor"])
    with pytest.raises(ValueError):
        model.inbox("active", 1, "garbage")
    db_session.get(ConversationState, "default").current_thread_id = "other"
    with pytest.raises(ValueError):
        model.inbox("active", 1, first["next_cursor"])
    db_session.get(ConversationState, "default").current_thread_id = "thread"


def test_scan_cap_resumes(db_session):
    model = seed(db_session, 2050)
    first = model.inbox("ended", 1)
    assert first["items"] == [] and first["has_more"]
    second = model.inbox("ended", 1, first["next_cursor"])
    assert second == {"items": [], "has_more": False, "next_cursor": None}


def test_empty_inbox_does_not_create_thread(db_session):
    assert WorkReadModel(db_session).inbox()["items"] == []
    assert db_session.query(Thread).count() == 0


def test_timeline_raw_highwater(db_session):
    model = seed(db_session, 1)
    for seq, kind, visibility in [
        (1, "task_created", "task"),
        (2, "run_started", "internal"),
        (3, "unknown", "task"),
    ]:
        db_session.add(
            AgentTaskEvent(
                task_id="00000",
                sequence=seq,
                event_type=kind,
                visibility=visibility,
                payload={"secret": "SECRET"},
            )
        )
    db_session.flush()
    result = model.timeline("00000", 0, 2)
    assert result["next_sequence"] == 2 and result["has_more"]
    assert len(result["items"]) == 1
    assert "SECRET" not in str(result)
    assert model.timeline("00000", 2, 2)["next_sequence"] == 3
    view = model.work_view(db_session.get(AgentTask, "00000"))
    assert "task_state" not in str(view)
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_route_contract(db_session):
    from aiive.api.routes_agent_tasks import router
    from aiive.db.base import get_db
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    seed(db_session, 1)
    client = TestClient(app)
    assert (
        client.get("/api/agent-tasks/inbox?bucket=active").json()["items"][0]["task_id"]
        == "00000"
    )
    assert client.get("/api/agent-tasks/inbox?bucket=bad").status_code == 422
    assert client.get("/api/agent-tasks/inbox?limit=51").status_code == 422
    assert client.get("/api/agent-tasks/inbox?cursor=garbage").status_code == 400
    assert client.get("/api/agent-tasks/00000/work-view").status_code == 200
    assert client.get("/api/agent-tasks/missing/timeline").status_code == 404


def test_batch_questions_32_and_lifecycle(db_session):
    from datetime import timedelta

    from aiive.task_runtime.goal_questions import (
        list_open_questions,
        list_open_questions_for_tasks,
        record_question,
        resolve_question,
    )

    seed(db_session, 1)
    task = db_session.get(AgentTask, "00000")
    task.task_state = {
        "goal": {
            "status": "active",
            "steps": [
                {"id": f"s{i}", "title": "step", "status": "waiting_user"}
                for i in range(32)
            ],
        }
    }
    db_session.flush()
    for i in range(32):
        record_question(
            db_session,
            task_id=task.id,
            step_id=f"s{i}",
            decision_id="d",
            trigger_key="one",
            question=f"q{i}",
        )
    questions = list_open_questions_for_tasks(db_session, [task.id])[task.id]
    assert len(questions) == 32
    assert len(list_open_questions(db_session, "thread")) == 5
    from aiive.db.models import Event

    db_session.add(
        Event(
            id="user",
            thread_id="thread",
            trace_id="test",
            event_type="user_message",
            payload={"text": "yes"},
        )
    )
    db_session.flush()
    resolve_question(
        db_session,
        question=questions[0],
        user_event_id="user",
        answer="yes",
        intent="answer",
    )
    assert len(list_open_questions_for_tasks(db_session, [task.id])[task.id]) == 31
    record_question(
        db_session,
        task_id=task.id,
        step_id=questions[1].step_id,
        decision_id="d",
        trigger_key="two",
        question="replacement",
    )
    current = list_open_questions_for_tasks(db_session, [task.id])[task.id]
    assert len(current) == 31
    assert questions[1].question_id not in {q.question_id for q in current}
    assert (
        list_open_questions_for_tasks(
            db_session, [task.id], now=datetime.now(UTC) + timedelta(days=4)
        )[task.id]
        == []
    )
    task.task_state = {"goal": {**task.task_state["goal"], "status": "paused"}}
    db_session.flush()
    assert list_open_questions_for_tasks(db_session, [task.id])[task.id] == []


def test_reads_issue_no_business_writes(db_session):
    from sqlalchemy import event

    model = seed(db_session, 1)
    task = db_session.get(AgentTask, "00000")
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        model.inbox("active")
        model.work_view(task)
        model.timeline(task.id)
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert statements and set(statements) <= {"SELECT"}
    assert not db_session.new and not db_session.dirty and not db_session.deleted
