from aiive.api.ws_manager import ws_manager
from aiive.db.models import Thread
from aiive.task_runtime.goal_service import GoalService
from aiive.task_runtime.repository import TaskRepository


def test_work_signals_follow_postgres_savepoint_and_outer_transaction(pg_sessionmaker, monkeypatch):
    sent = []
    monkeypatch.setattr(ws_manager,"broadcast_to_thread_sync",lambda *args: sent.append(args))
    with pg_sessionmaker() as db:
        db.add(Thread(id="thread"))
        db.commit()
        repo = TaskRepository(db)
        task = repo.create_task(thread_id="thread",goal="test",enqueue=False)
        db.commit()
        sent.clear()
        repo.append_event(task.id,"internal_one",{},visibility="internal")
        nested = db.begin_nested()
        repo.append_event(task.id,"discard",{},visibility="internal")
        nested.rollback()
        nested = db.begin_nested()
        repo.append_event(task.id,"internal_two",{},visibility="internal")
        nested.commit()
        assert not sent
        db.commit()
        work = [item for item in sent if item[1] == "work_changed"]
        assert len(work) == 1 and work[0][2]["event_sequence"] == 3
        sent.clear()
        repo.append_event(task.id,"rolled_back",{},visibility="internal")
        db.rollback()
        assert not sent


def test_notification_count_after_real_commit_only(pg_sessionmaker, monkeypatch):
    counts = []
    monkeypatch.setattr(ws_manager,"broadcast_notification_sync",lambda count: counts.append(count))
    with pg_sessionmaker() as db:
        db.add(Thread(id="thread"))
        db.commit()
        task = TaskRepository(db).create_task(thread_id="thread",goal="test",enqueue=False)
        db.commit()
        counts.clear()
        nested = db.begin_nested()
        GoalService(db)._notify(task,title="a",content="a",category="test",dedupe="one")
        nested.rollback()
        db.commit()
        assert counts == []
        GoalService(db)._notify(task,title="b",content="b",category="test",dedupe="two")
        assert counts == []
        db.commit()
        assert counts == [1]


def test_failed_answer_rolls_back_source_and_signals_on_postgres(pg_sessionmaker, monkeypatch):
    import pytest
    from fastapi import HTTPException
    from aiive.api.routes_agent_tasks import AnswerGoalQuestionRequest, answer_goal_step_question
    from aiive.db.models import Event
    from aiive.task_runtime.goal_service import GoalError
    sent = []
    monkeypatch.setattr(ws_manager,"broadcast_to_thread_sync",lambda *args: sent.append(args))
    with pg_sessionmaker() as db:
        db.add(Thread(id="thread"))
        db.commit()
        task = TaskRepository(db).create_task(thread_id="thread",goal="test",task_type="long_term_goal",enqueue=False)
        task.task_state = {"goal":{"status":"active","steps":[{"id":"step","title":"step","status":"waiting_user","note":"question"}]}}
        db.commit()
        sent.clear()
        before = db.query(Event).filter_by(event_type="task_input").count()
        def reject(*args, **kwargs):
            raise GoalError("step_requires_approval")
        monkeypatch.setattr(GoalService,"answer_step_question",reject)
        with pytest.raises(HTTPException):
            answer_goal_step_question(task.id,AnswerGoalQuestionRequest(step_id="step",answer="yes"),db)
        db.commit()
        assert db.query(Event).filter_by(event_type="task_input").count() == before
        assert sent == []
