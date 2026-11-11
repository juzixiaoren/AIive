from aiive.db.models import AgentTaskEvent
from aiive.task_runtime.goal_questions import list_open_questions_for_tasks

from tests.integration.backend.test_work_inbox_api import seed


def test_hidden_children_cannot_supply_question_or_timeline(db_session, monkeypatch):
    model = seed(db_session, 1)
    db_session.add(
        AgentTaskEvent(
            task_id="00000",
            sequence=1,
            event_type="task_blocked_user",
            payload={"question": "SECRET"},
        )
    )
    db_session.flush()
    monkeypatch.setattr(type(model.reader), "visible_child", lambda *args: False)
    assert model.timeline("00000")["items"] == []
    assert model.inbox("attention")["items"] == []
    assert list_open_questions_for_tasks(db_session, ["00000"]) == {"00000": []}


def test_hidden_task_omitted(db_session, monkeypatch):
    model = seed(db_session, 1)
    monkeypatch.setattr(type(model.reader), "task_visible", lambda *args: False)
    assert model.inbox("active")["items"] == []
    assert model.reader.get_task("00000") is None


def test_real_forget_shield_hides_work_task(db_session):
    from aiive.db.models import ConversationState
    from aiive.task_runtime.work_read_model import WorkReadModel

    from tests.integration.backend.test_forget_goal_read_bypasses import forget, goal

    thread, task, original = goal(db_session)
    task_id = task.id
    state = db_session.get(ConversationState, "default")
    if state:
        state.current_thread_id = thread.id
    else:
        db_session.add(ConversationState(id="default", current_thread_id=thread.id))
    db_session.commit()
    model = WorkReadModel(db_session)
    assert model.reader.get_task(task_id) is not None
    forget(db_session, original)
    assert model.reader.get_task(task_id) is None
    for bucket in ["attention", "active", "waiting", "ended"]:
        assert model.inbox(bucket)["items"] == []


def test_real_forget_shield_hides_question(db_session):
    from aiive.forget.durable_sources import persist_sources
    from aiive.task_runtime.work_read_model import WorkReadModel

    from tests.integration.backend.test_forget_goal_read_bypasses import (
        forget,
        goal,
        signal,
    )
    from tests.unit.backend.test_goal_question_state_consistency import _ask

    thread, task, _ = goal(db_session)
    question_id = _ask(db_session, task.id, "next", "PRIVATE_QUESTION")["question_id"]
    event = (
        db_session.query(AgentTaskEvent)
        .filter_by(task_id=task.id, event_type="goal_question_asked")
        .one()
    )
    original = signal(db_session, thread.id)
    persist_sources(
        db_session, "agent_task_event", event.id, {"source_event_id": original.id}
    )
    db_session.commit()
    assert question_id in str(list_open_questions_for_tasks(db_session, [task.id]))
    forget(db_session, original)
    assert list_open_questions_for_tasks(db_session, [task.id])[task.id] == []
    view = WorkReadModel(db_session).work_view(task)
    assert question_id not in str(view)
    assert "PRIVATE_QUESTION" not in str(view)


def test_real_forget_shield_hides_action_evidence_artifact(db_session):
    from aiive.db.models import AgentAction, AgentRun, TaskArtifact, TaskEvidence
    from aiive.forget.durable_sources import persist_sources
    from aiive.task_runtime.work_read_model import WorkReadModel

    from tests.integration.backend.test_forget_goal_read_bypasses import (
        forget,
        goal,
        signal,
    )

    thread, task, _ = goal(db_session)
    original = signal(db_session, thread.id)
    db_session.add(AgentRun(id="run", task_id=task.id, run_index=1))
    db_session.flush()
    action = AgentAction(
        id="action",
        task_id=task.id,
        run_id="run",
        sequence=1,
        capability_id="run_shell",
        arguments_hash="a",
        descriptor_hash="d",
        idempotency_key="key",
        status="running",
    )
    evidence = TaskEvidence(id="evidence", task_id=task.id, content_hash="hash")
    artifact = TaskArtifact(
        id="artifact",
        task_id=task.id,
        name="file",
        object_bucket="bucket",
        object_key="key",
        content_hash="hash",
    )
    db_session.add_all([action, evidence, artifact])
    db_session.flush()
    for owner, row in [
        ("agent_action", action),
        ("task_evidence", evidence),
        ("task_artifact", artifact),
    ]:
        persist_sources(db_session, owner, row.id, {"source_event_id": original.id})
    db_session.commit()
    model = WorkReadModel(db_session)
    view = model.work_view(task)
    assert view["current_activity"]["action_id"] == "action"
    assert [a["id"] for a in view["actions"]] == ["action"]
    assert "arguments" not in view["actions"][0]
    assert view["evidence"][0]["id"] == "evidence"
    assert view["artifacts"][0]["id"] == "artifact"
    forget(db_session, original)
    view = model.work_view(task)
    assert view["current_activity"] is None
    assert view["actions"] == []
    assert view["evidence"] == [] and view["artifacts"] == []


def test_work_changed_respects_real_forget_shield(db_session, monkeypatch):
    from aiive.api.ws_manager import ws_manager
    from aiive.task_runtime.goal_service import GoalService
    from aiive.task_runtime.read_repository import TaskReadRepository
    from aiive.task_runtime.repository import TaskRepository
    from tests.integration.backend.test_forget_goal_read_bypasses import forget, goal

    sent = []
    monkeypatch.setattr(ws_manager, "broadcast_to_thread_sync", lambda *args: sent.append(args))
    thread, hidden, source = goal(db_session)
    repo = TaskRepository(db_session)
    visible = repo.create_task(thread_id=thread.id, goal="visible", enqueue=False)
    db_session.commit()
    assert any(e[1] == "work_changed" and e[2]["task_id"] == visible.id for e in sent)
    plan = GoalService(db_session).load_plan(hidden)
    assert plan is not None
    forget(db_session, source)
    assert not TaskReadRepository(db_session).task_visible(hidden)
    assert TaskReadRepository(db_session).task_visible(visible)
    sent.clear()
    repo.append_event(hidden.id, "hidden_event", {}, visibility="internal")
    repo.transition_task(hidden, "dispatching")
    GoalService(db_session)._save(hidden, plan)
    repo.append_event(visible.id, "visible_event", {}, visibility="internal")
    db_session.commit()
    ids = [payload["task_id"] for _, kind, payload in sent if kind == "work_changed"]
    assert ids == [visible.id]


def test_work_changed_fails_closed_on_visibility_errors(db_session, monkeypatch):
    from aiive.api.ws_manager import ws_manager
    from aiive.task_runtime.read_repository import TaskReadRepository
    from aiive.task_runtime.repository import queue_work_changed
    from tests.integration.backend.test_work_inbox_api import seed

    model = seed(db_session, 1)
    task = model.reader.get_task("00000")
    sent = []
    monkeypatch.setattr(ws_manager, "broadcast_to_thread_sync", lambda *args: sent.append(args))
    def fail(*args):
        raise RuntimeError("visibility unavailable")
    monkeypatch.setattr(TaskReadRepository, "task_visible", fail)
    queue_work_changed(db_session, task)
    db_session.commit()
    assert not [entry for entry in sent if entry[1] == "work_changed"]


def test_reminder_id_mapping_is_batched_and_distinguishes_goal(db_session):
    from sqlalchemy import event as sa_event
    from aiive.db.models import Event, Task, Thread
    from aiive.api.routes_notifications import list_notifications
    from aiive.task_runtime.repository import TaskRepository

    thread = Thread(id="notification-batch")
    db_session.add(thread)
    db_session.flush()
    reminder = Task(id="reminder", task_type="reminder", title="reminder")
    other = Task(id="other", task_type="routine", title="other")
    db_session.add_all([reminder, other])
    db_session.flush()
    goal = TaskRepository(db_session).create_task(thread_id=thread.id, goal="goal", enqueue=False)
    payloads = {
        "old": {"task_id": reminder.id},
        "new": {"reminder_task_id": reminder.id},
        "routine": {"task_id": other.id},
        "agent": {"task_id": goal.id},
        "goal_old": {"goal_key": "goal:key", "task_id": goal.id},
        "goal_new": {"agent_task_id": goal.id, "reminder_task_id": reminder.id},
    }
    for key, payload in payloads.items():
        db_session.add(Event(id=key, thread_id=thread.id, trace_id=key,
                             event_type="notification_created", payload={"status":"pending", **payload}))
    db_session.flush()
    queries = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if "FROM tasks" in statement:
            queries.append(statement)
    conn = db_session.connection()
    sa_event.listen(conn, "before_cursor_execute", capture)
    try:
        rows = {item["id"]: item for item in list_notifications(db=db_session)}
    finally:
        sa_event.remove(conn, "before_cursor_execute", capture)
    assert rows["old"]["reminder_task_id"] == reminder.id
    assert rows["new"]["reminder_task_id"] == reminder.id
    for key in ("routine", "agent", "goal_old", "goal_new"):
        assert rows[key]["reminder_task_id"] is None
    assert rows["goal_old"]["agent_task_id"] == goal.id
    assert rows["goal_new"]["agent_task_id"] == goal.id
    assert len(queries) == 1
