from datetime import timedelta

import pytest
from fastapi import HTTPException

from aiive.api.routes_agent_tasks import AnswerGoalQuestionRequest, answer_goal_step_question
from aiive.api.routes_notifications import delete_notification, list_notifications
from aiive.db.models import AgentTaskEvent, Event
from aiive.task_runtime.goal_questions import load_question
from aiive.task_runtime.goal_service import GoalService
from aiive.task_runtime.repository import TaskRepository, utcnow
from tests.unit.backend.test_goal_question_state_consistency import _ask, _goal, _thread


def goal_and_question(db):
    thread = _thread(db)
    task_id = _goal(db, thread, [{"id":"step", "title":"step", "kind":"autonomous"}])
    question_id = _ask(db, task_id, "step", "question")["question_id"]
    return thread, task_id, question_id


@pytest.mark.parametrize("intent,changes", [("answer",None), ("reject",None), ("modify_plan",{"summary":"updated"})])
def test_answer_api(db_session, intent, changes):
    _, task_id, question_id = goal_and_question(db_session)
    request = AnswerGoalQuestionRequest(step_id="step",question_id=question_id,answer="yes",intent=intent,plan_changes=changes)
    result = answer_goal_step_question(task_id,request,db_session)
    assert result["status"] == "applied"
    assert load_question(db_session,question_id).status == "resolved"
    assert db_session.get(Event,result["user_event_id"]).event_type == "task_input"


def test_invalid_or_missing_question_never_creates_input(db_session):
    _, task_id, question_id = goal_and_question(db_session)
    before = db_session.query(Event).filter_by(event_type="task_input").count()
    for step, question in [("step",None),("other",question_id),("step","not-found")]:
        with pytest.raises(HTTPException):
            answer_goal_step_question(task_id,AnswerGoalQuestionRequest(step_id=step,question_id=question,answer="yes"),db_session)
    assert db_session.query(Event).filter_by(event_type="task_input").count() == before


def test_legacy_answer_and_approval_boundary(db_session):
    thread = _thread(db_session)
    task_id = _goal(db_session,thread,[{"id":"step","title":"step","kind":"autonomous"}])
    GoalService(db_session).mark_step_waiting_user(task_id,"step","legacy question")
    db_session.commit()
    result = answer_goal_step_question(task_id,AnswerGoalQuestionRequest(step_id="step",answer="yes"),db_session)
    assert result["status"] == "applied"
    task_id = _goal(db_session,thread,[{"id":"step","title":"step","kind":"approval_required"}])
    question_id = _ask(db_session,task_id,"step","approval needed")["question_id"]
    before = db_session.query(Event).filter_by(event_type="task_input").count()
    with pytest.raises(HTTPException):
        answer_goal_step_question(task_id,AnswerGoalQuestionRequest(step_id="step",question_id=question_id,answer="yes"),db_session)
    assert db_session.query(Event).filter_by(event_type="task_input").count() == before


def test_work_changed_commit_savepoints(db_session, monkeypatch):
    from aiive.api.ws_manager import ws_manager
    sent = []
    monkeypatch.setattr(ws_manager,"broadcast_to_thread_sync",lambda thread, kind, payload: sent.append((thread,kind,payload)))
    thread = _thread(db_session)
    task = TaskRepository(db_session).create_task(thread_id=thread,goal="test",enqueue=False)
    db_session.commit()
    sent.clear()
    repo = TaskRepository(db_session)
    repo.append_event(task.id,"first",{"secret":"private"},visibility="internal")
    with db_session.begin_nested():
        repo.append_event(task.id,"second",{},visibility="internal")
    assert not sent
    nested = db_session.begin_nested()
    repo.append_event(task.id,"rolled_back",{},visibility="internal")
    nested.rollback()
    db_session.commit()
    work = [entry for entry in sent if entry[1] == "work_changed"]
    assert len(work) == 1 and work[0][2]["event_sequence"] == 3
    assert "private" not in str(work)
    assert not [entry for entry in sent if entry[1] == "task_event"]


def test_goal_notifications_never_cancel_reminders(db_session, monkeypatch):
    from aiive.runtime.task_manager import TaskManager
    thread = _thread(db_session)
    task_id = _goal(db_session,thread,[{"id":"step","title":"step"}])
    task = TaskRepository(db_session).get_task(task_id)
    notification_id = GoalService(db_session)._notify(task,title="title",content="content",category="goal_test",dedupe="one")
    db_session.commit()
    item = next(x for x in list_notifications(db=db_session) if x["id"] == notification_id)
    assert item["agent_task_id"] == task_id and item["category"] == "goal_test"
    monkeypatch.setattr(TaskManager,"cancel",lambda *args: pytest.fail("must not cancel reminder"))
    assert delete_notification(notification_id,db_session)["ok"]


def test_paused_goal_blocks_approval_and_expiry_then_resumes(db_session):
    from aiive.control.approval_service import TaskApprovalService
    from aiive.db.models import AgentAction, AgentRun, ApprovalRequest, OutboxJob
    thread = _thread(db_session)
    task = TaskRepository(db_session).create_task(thread_id=thread,goal="goal",task_type="long_term_goal",enqueue=False)
    task.status = "blocked_approval"
    task.task_state = {"goal":{"status":"paused","steps":[{"id":"step","title":"step"}]}}
    run = AgentRun(id="approval-run",task_id=task.id,run_index=1)
    db_session.add(run)
    db_session.flush()
    action = AgentAction(id="approval-action",task_id=task.id,run_id=run.id,sequence=1,capability_id="run_shell",arguments_hash="hash",descriptor_hash="descriptor",idempotency_key="approval-key",status="awaiting_approval")
    db_session.add(action)
    db_session.flush()
    approval = ApprovalRequest(id="approval",thread_id=thread,task_id=task.id,action_id=action.id,tool_name="run_shell",tool_args={},tool_args_hash="hash",descriptor_hash="descriptor",expires_at=utcnow()-timedelta(seconds=1),status="pending")
    db_session.add(approval)
    db_session.flush()
    service = TaskApprovalService(db_session)
    before = db_session.query(OutboxJob).count()
    for goal in ([{"status": status, "steps": [{"id": "step", "title": "step"}]} for status in ("paused", "blocked", "completed")] + [None, {"status": "corrupt"}]):
        task.task_state = {"goal": goal}
        db_session.flush()
        for decision in ("approve", "deny"):
            with pytest.raises(ValueError,match="goal_not_active"):
                service.decide(approval.id,decision)
        assert service.expire_due() == 0
        assert (task.status,action.status,approval.status) == ("blocked_approval","awaiting_approval","pending")
        assert db_session.query(OutboxJob).count() == before
    task.task_state = {"goal":{"status":"active","steps":[{"id":"step","title":"step"}]}}
    db_session.flush()
    assert service.expire_due() == 1
    assert (task.status,action.status,approval.status) == ("queued","invalidated","failed")


def test_broadcast_failure_does_not_prevent_commit(db_session, monkeypatch):
    from aiive.api.ws_manager import ws_manager
    sent = []
    monkeypatch.setattr(ws_manager,"broadcast_to_thread_sync",lambda *args: sent.append(args))
    thread = _thread(db_session)
    task = TaskRepository(db_session).create_task(thread_id=thread,goal="test",enqueue=False)
    db_session.commit()
    task_id = task.id
    sent.clear()
    def fail(*args):
        raise RuntimeError("socket unavailable")
    monkeypatch.setattr(ws_manager,"broadcast_to_thread_sync",fail)
    TaskRepository(db_session).append_event(task_id,"committed",{},visibility="internal")
    db_session.commit()
    assert db_session.query(AgentTaskEvent).filter_by(task_id=task_id,event_type="committed").count() == 1


def test_stale_and_superseded_questions_rejected_by_api(db_session):
    thread, task_id, question_id = goal_and_question(db_session)
    newer = _ask(db_session,task_id,"step","new question",trigger_key="new-trigger")["question_id"]
    assert newer != question_id
    request = AnswerGoalQuestionRequest(step_id="step",question_id=question_id,answer="yes")
    with pytest.raises(HTTPException):
        answer_goal_step_question(task_id,request,db_session)
    GoalService(db_session).pause(task_id)
    db_session.commit()
    request = AnswerGoalQuestionRequest(step_id="step",question_id=newer,answer="yes")
    with pytest.raises(HTTPException):
        answer_goal_step_question(task_id,request,db_session)


def test_old_goal_notification_does_not_cancel(db_session,monkeypatch):
    from aiive.runtime.task_manager import TaskManager
    thread = _thread(db_session)
    old = Event(thread_id=thread,trace_id="goal",event_type="notification_created",payload={"goal_key":"goal:key", "task_id":"goal", "category":"goal_old", "status":"pending"})
    db_session.add(old)
    db_session.flush()
    assert next(item for item in list_notifications(db=db_session) if item["id"] == old.id)["agent_task_id"] == "goal"
    monkeypatch.setattr(TaskManager,"cancel",lambda *args: pytest.fail("must not cancel reminder"))
    assert delete_notification(old.id,db_session)["ok"]


def test_goal_save_and_transition_without_event_emit_work_signal(db_session, monkeypatch):
    from aiive.api.ws_manager import ws_manager
    from aiive.db.models import AgentTask
    sent = []
    monkeypatch.setattr(ws_manager,"broadcast_to_thread_sync",lambda *args: sent.append(args))
    thread = _thread(db_session)
    task_id = _goal(db_session,thread,[{"id":"step","title":"step"}])
    sent.clear()
    task = db_session.get(AgentTask,task_id)
    service = GoalService(db_session)
    plan = service.load_plan(task)
    plan.summary = "updated"
    service._save(task,plan)
    TaskRepository(db_session).transition_task(task,"dispatching")
    assert sent == []
    db_session.commit()
    work = [entry for entry in sent if entry[1] == "work_changed"]
    assert len(work) == 1 and work[0][2]["task_version"] == task.version


def test_expired_question_rejected_without_creating_input(db_session):
    _, task_id, question_id = goal_and_question(db_session)
    event = db_session.query(AgentTaskEvent).filter_by(task_id=task_id,event_type="goal_question_asked").one()
    event.payload = {**event.payload,"expires_at":(utcnow()-timedelta(seconds=1)).isoformat()}
    db_session.flush()
    before = db_session.query(Event).filter_by(event_type="task_input").count()
    with pytest.raises(HTTPException):
        answer_goal_step_question(task_id,AnswerGoalQuestionRequest(step_id="step",question_id=question_id,answer="yes"),db_session)
    assert db_session.query(Event).filter_by(event_type="task_input").count() == before


def test_active_goal_action_approval_uses_existing_service(db_session,tmp_path):
    from aiive.control.approval_service import TaskApprovalService
    from aiive.db.models import ApprovalRequest, Thread
    from aiive.task_runtime.runtime import PersistentTaskRuntime
    from aiive.task_runtime.schemas import ActionProposal
    from tests.unit.backend.test_persistent_task_runtime import _task, SequenceAgent
    target = tmp_path / "delete.txt"
    target.write_text("data")
    task = _task(db_session,db_session.get(Thread,_thread(db_session)),task_type="file_operation",task_brief={"scope":{"allowed_capabilities":["safe_delete"],"allowed_roots":[str(tmp_path)]}})
    PersistentTaskRuntime(db_session,agent_executor=SequenceAgent(ActionProposal(decision="action",capability_id="safe_delete",arguments={"path":str(target),"scope_id":"test_artifacts","mode":"trash"}))).run(task.id)
    approval = db_session.query(ApprovalRequest).filter_by(task_id=task.id).one()
    task.task_type = "long_term_goal"
    task.task_state = {**task.task_state,"goal":{"status":"active","steps":[{"id":"step","title":"step"}]}}
    db_session.flush()
    assert TaskApprovalService(db_session).decide(approval.id,"approve")["action"] == "succeeded"


def test_answer_route_http_contract(db_session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from aiive.api.routes_agent_tasks import router
    from aiive.db.base import get_db
    _, task_id, question_id = goal_and_question(db_session)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)
    url = f"/api/agent-tasks/{task_id}/goal/questions/answer"
    assert client.post(url,json={"step_id":"step","question_id":question_id,"answer":"yes"}).status_code == 200
    assert client.post(url,json={"step_id":"step","question_id":question_id,"answer":"yes"}).status_code == 409
