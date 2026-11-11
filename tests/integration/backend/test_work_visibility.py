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
    assert question_id not in str(WorkReadModel(db_session).work_view(task))


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
    assert view["evidence"][0]["id"] == "evidence"
    assert view["artifacts"][0]["id"] == "artifact"
    forget(db_session, original)
    view = model.work_view(task)
    assert view["current_activity"] is None
    assert view["evidence"] == [] and view["artifacts"] == []
