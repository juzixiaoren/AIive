import pytest

from aiive.db.forget_models import ContentProvenanceRef
from aiive.db.models import AgentTask, AgentTaskEvent
from aiive.task_runtime.goal_questions import list_open_questions_for_tasks

from tests.integration.backend.test_work_inbox_api import seed


def test_hidden_children_cannot_supply_question_or_timeline(db_session):
    from tests.integration.backend.test_forget_goal_read_bypasses import signal, forget

    model = seed(db_session, 1)
    task = db_session.get(AgentTask, "00000")
    task.status = "blocked_user"
    origin = signal(db_session, "thread")
    db_session.add_all([
        AgentTaskEvent(id="question", task_id=task.id, sequence=1, event_type="task_blocked_user",
                       payload={"question": "SECRET"}),
        ContentProvenanceRef(owner_type="agent_task_event", owner_id="question",
                             source_type="event", source_id=origin.id),
    ])
    db_session.flush()
    assert model.inbox("attention")["items"]
    forget(db_session, origin)
    assert model.timeline(task.id)["items"] == []
    assert model.inbox("attention")["items"] == []


def test_task_without_legacy_proof_is_omitted(db_session):
    model = seed(db_session, 1)
    task = db_session.get(AgentTask, "00000")
    task.task_state = {"inputs": ["unproven"]}
    db_session.flush()
    assert model.inbox("active")["items"] == []
    assert model.reader.get_task(task.id) is None


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


@pytest.mark.parametrize("proof_kind", ["unknown", "dangling", "none", "forgotten", "resolution_error"])
def test_action_and_event_require_visible_evidence_even_with_own_proof(db_session, monkeypatch, proof_kind):
    from aiive.db.models import AgentAction, AgentRun, TaskEvidence
    from tests.integration.backend.test_forget_goal_read_bypasses import signal, forget

    model = seed(db_session, 1)
    origin = signal(db_session, "thread")
    other = signal(db_session, "thread")
    db_session.add(AgentRun(id="run", task_id="00000", run_index=1))
    db_session.flush()
    db_session.add_all([
        TaskEvidence(id="evidence", task_id="00000", content_hash="evidence"),
        AgentAction(id="action", task_id="00000", run_id="run", sequence=1, capability_id="run_shell",
                    arguments_hash="args", descriptor_hash="descr", idempotency_key="action",
                    status="succeeded", result_summary={"evidence_id": "evidence"}),
        AgentTaskEvent(id="event", task_id="00000", action_id="action", sequence=1,
                       event_type="action_succeeded"),
        ContentProvenanceRef(owner_type="agent_action", owner_id="action",
                             source_type="event", source_id=other.id),
        ContentProvenanceRef(owner_type="agent_task_event", owner_id="event",
                             source_type="event", source_id=other.id),
    ])
    if proof_kind != "none":
        db_session.add(ContentProvenanceRef(
            owner_type="task_evidence", owner_id="evidence",
            source_type="unknown" if proof_kind == "unknown" else "event",
            source_id="missing" if proof_kind == "dangling" else origin.id,
        ))
    db_session.flush()
    if proof_kind == "forgotten":
        assert model.timeline("00000")["items"]
        forget(db_session, origin)
    if proof_kind == "resolution_error":
        from aiive.forget.visibility_service import ForgetVisibilityService

        def unavailable(*args):
            raise RuntimeError("source metadata unavailable")

        monkeypatch.setattr(ForgetVisibilityService, "load_target_meta", staticmethod(unavailable))
    view = model.work_view("00000")
    assert view["actions"] == view["timeline"] == view["evidence"] == []
    assert model.timeline("00000") == {"items": [], "next_sequence": 1, "has_more": False}


def test_dangling_memory_proof_does_not_hide_independent_evidence(db_session):
    from aiive.db.models import MemoryRecord, TaskEvidence
    from tests.integration.backend.test_forget_goal_read_bypasses import signal

    model = seed(db_session, 1)
    origin = signal(db_session, "thread")
    db_session.add(MemoryRecord(id="present", memory_type="fact", content="visible"))
    for key in ("present", "removed"):
        db_session.add_all([
            TaskEvidence(id=key, task_id="00000", content_hash=key),
            ContentProvenanceRef(owner_type="task_evidence", owner_id=key,
                                 source_type="memory_record", source_id=key),
            ContentProvenanceRef(owner_type="memory_record", owner_id=key,
                                 source_type="event", source_id=origin.id),
        ])
    db_session.flush()
    assert [item["id"] for item in model.metadata_page("00000", "evidence")["items"]] == ["present"]


@pytest.mark.parametrize("changes", [1, 2])
def test_work_read_rechecks_committed_forget(_db_template, tmp_path, changes):
    from shutil import copyfile

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session

    from aiive.api.routes_agent_tasks import router
    from aiive.db.base import get_db
    from aiive.db.models import Event
    from tests.integration.backend.test_forget_goal_read_bypasses import forget, signal

    database = tmp_path / "work.db"
    copyfile(_db_template, database)
    engine = create_engine(f"sqlite:///{database}")
    try:
        with Session(engine) as setup:
            seed(setup, 1)
            origin = signal(setup, "thread").id
            newer = signal(setup, "thread").id
            setup.add_all([
                AgentTaskEvent(id="private", task_id="00000", sequence=1, event_type="task_created"),
                ContentProvenanceRef(owner_type="agent_task_event", owner_id="private",
                                     source_type="event", source_id=origin),
            ])
            setup.commit()

        app = FastAPI()
        app.include_router(router)
        with Session(engine) as reading, Session(engine) as writing:
            app.dependency_overrides[get_db] = lambda: reading
            revision_queries = 0

            def before_read(conn, cursor, statement, parameters, context, executemany):
                nonlocal revision_queries
                if "count(forget_operations.id)" not in statement.lower():
                    return
                revision_queries += 1
                if revision_queries == 2 or (revision_queries == 4 and changes == 2):
                    identity = origin if revision_queries == 2 else newer
                    forget(writing, writing.get(Event, identity))

            event.listen(reading.connection(), "before_cursor_execute", before_read)
            with TestClient(app) as client:
                result = client.get("/api/agent-tasks/00000/work-view")
            assert revision_queries == 4
            if changes == 1:
                assert result.status_code == 200
                assert result.json()["timeline"] == []
                assert result.json()["visibility_revision"] == 1
            else:
                assert result.status_code == 503
                assert result.json() == {"detail": "work_visibility_changed_retry"}
                assert result.headers["Retry-After"] == "1"
    finally:
        engine.dispose()


def test_inherit_sources_batches_typed_parents_and_preserves_existing(db_session):
    from sqlalchemy import event
    from aiive.task_runtime.read_repository import inherit_sources

    db = db_session
    def proof(owner, identity, source):
        return ContentProvenanceRef(owner_type=owner, owner_id=identity, source_type="event", source_id=source)

    parents = [("task_evidence", str(i)) for i in range(70)]
    db.add_all([proof(kind, identity, "shared") for kind, identity in parents])
    db.add_all([proof("agent_action", "70", "action-source"), proof("agent_action", "0", "unrelated"),
                proof("agent_task", "target", "original")])
    parents += [("agent_action", "70"), parents[0]]
    selects = []
    def capture(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)
    conn = db.connection()
    event.listen(conn, "before_cursor_execute", capture)
    try:
        inherit_sources(db, "agent_task", "target", parents)
    finally:
        event.remove(conn, "before_cursor_execute", capture)
    assert len(selects) == 2
    inherit_sources(db, "agent_task", "target", parents)
    refs = db.query(ContentProvenanceRef).filter_by(owner_type="agent_task", owner_id="target")
    assert sorted(ref.source_id for ref in refs) == ["action-source", "original", "shared"]


def test_recent_evidence_skips_hidden_batches_and_stops_early(db_session):
    from datetime import UTC, datetime
    from sqlalchemy import event
    from aiive.db.models import TaskEvidence
    from aiive.task_runtime.read_repository import TaskReadRepository
    from tests.integration.backend.test_forget_goal_read_bypasses import signal

    db = db_session
    seed(db, 1)
    origin = signal(db, "thread")
    stamp = datetime.now(UTC)
    for i in range(200):
        identity = f"{i:03d}"
        db.add(TaskEvidence(id=identity, task_id="00000", content_hash="hash", created_at=stamp))
        if i < 160:
            db.add(ContentProvenanceRef(owner_type="task_evidence", owner_id=identity,
                                        source_type="event", source_id=origin.id))
    db.flush()
    db.expunge_all()
    loaded = []
    def track(_, row):
        if isinstance(row, TaskEvidence):
            loaded.append(row.id)
    event.listen(db, "loaded_as_persistent", track)
    try:
        recent = TaskReadRepository(db).recent_evidence("00000", 3)
    finally:
        event.remove(db, "loaded_as_persistent", track)
    assert [row.id for row in recent] == ["159", "158", "157"]
    assert 40 < len(loaded) < 200
