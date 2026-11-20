"""Offline end-to-end delivery with the real Worker, Inbox, Turn and tool registry."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from functools import wraps
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from aiive.api.ws_manager import ws_manager
from aiive.core.llm_client import FakeLLMClient
from aiive.db.forget_models import ContentProvenanceRef, ForgetOperation, ForgetTombstone
from aiive.db.models import Event, OutboxJob, TurnEventInbox, TurnRecord
from aiive.runtime.agent_graph import AgentGraph
from aiive.runtime.conversation_state import get_current_thread_id
from aiive.runtime.turn_events import INTERNAL_CONTEXT_HEADER, claim_pending_events
from aiive.runtime.turn_execution import TurnExecutionService
from aiive.task_runtime.agent_executor import AgentDecision, TaskAgentExecutor
from aiive.task_runtime.repository import TaskRepository
from aiive.task_runtime.schemas import ActionProposal, TaskStatus
from aiive.task_runtime.tools import GetTaskStatusTool
from aiive.worker.handler_registry import HandlerRegistry
from aiive.worker.job_types import MAIN_AGENT_TASK_TERMINAL
from aiive.worker.outbox_handlers import register_all
from aiive.worker.outbox_heartbeat import ActiveClaimRegistry
from aiive.worker.outbox_worker import OutboxWorker

TERMINAL_JOBS = frozenset({MAIN_AGENT_TASK_TERMINAL})


def _worker() -> OutboxWorker:
    handlers = HandlerRegistry()
    register_all(handlers)
    return OutboxWorker("offline-worker", handlers, ActiveClaimRegistry())


def _tool_call(name, call_id, **args):
    return {"name": name, "id": call_id, "args": args}


def _injection(messages):
    return next((str(m.content) for m in reversed(messages)
                 if isinstance(m, SystemMessage) and str(m.content).startswith(INTERNAL_CONTEXT_HEADER)), "")


class ReportReviewer:
    def __init__(self):
        self.calls = []
        self.reports = []
        self.respond = self.review

    def bind_tools(self, _tools):
        return self

    def invoke(self, messages):
        self.calls.append(list(messages))
        return self.respond(messages)

    def review(self, messages):
        context = _injection(messages)
        assert "get_task_status" in context
        # Tool history can contain earlier Turns. Read exactly the IDs in this injection.
        task_ids = [line.split()[2] for line in context.splitlines() if line.startswith("- task_id:")]
        views = [json.loads(str(m.content)) for m in messages if isinstance(m, ToolMessage)
                 and m.name == "get_task_status"]
        reports = {v.get("task_id"): v for v in views if "report" in v}
        if not all(task_id in reports for task_id in task_ids):
            return AIMessage(content="", tool_calls=[
                _tool_call("get_task_status", f"status-{task_id}", task_id=task_id)
                for task_id in task_ids if task_id not in reports
            ])
        selected = [reports[task_id] for task_id in task_ids]
        for view in selected:
            assert view["goal"].startswith("原始目标")
            assert view["acceptance_criteria"] == ["需要可核验的证据"]
            assert view["usage"]["model_calls"] == 1 and view["budgets"]["max_runs"] > 0
        self.reports.append(selected)
        return AIMessage(content="\n".join(
            f"{view['status']} | {view['report']['summary']} | {view['report']['unresolved']}" for view in selected
        ))


@pytest.fixture
def reviewer(monkeypatch):
    model = ReportReviewer()
    monkeypatch.setattr(AgentGraph, "_build_langchain_llm", lambda _graph: model)
    monkeypatch.setattr("aiive.runtime.turn_execution.default_llm_client", FakeLLMClient)
    monkeypatch.setattr(ws_manager, "broadcast_to_thread_sync", Mock())
    return model


@pytest.fixture
def finish_worker_task(db, monkeypatch):
    def decide(_executor, task, _capabilities):
        status = task.task_brief["initial_input"]["result"]
        return AgentDecision(ActionProposal(decision="finish", report_status=status, summary="已运行",
                                            unresolved=["证据缺失"] if status != "succeeded" else []),
                             "offline", {"prompt_tokens": 20, "completion_tokens": 5}, {}, [])

    monkeypatch.setattr(TaskAgentExecutor, "decide", decide)
    monkeypatch.setattr(TaskAgentExecutor, "generate_report", lambda _self, task, _outcome, **_:
                        (f"真实报告 {task.title}", {"total_tokens": 50}, False))

    def finish(thread_id, status="succeeded", name="测试任务"):
        task = TaskRepository(db).create_task(
            thread_id=thread_id, goal=f"原始目标 {name}", title=name,
            task_brief={"acceptance_criteria": ["需要可核验的证据"], "initial_input": {"result": status}},
        )
        task_id = task.id
        db.commit()
        assert _worker().poll(only_types=frozenset({"agent_task_run"})) == 1
        db.expire_all()
        assert db.get(type(task), task_id).status == status
        return task_id

    return finish


def _jobs(db):
    db.expire_all()
    return db.query(OutboxJob).filter_by(job_type=MAIN_AGENT_TASK_TERMINAL)


def _make_due(db):
    _jobs(db).filter_by(status="pending").update({"available_at": None})
    db.commit()


@pytest.mark.parametrize("status", ["succeeded", "partial", "failed"])
def test_idle_turn_reads_actual_worker_report_once(db, reviewer, finish_worker_task, status):
    thread_id = get_current_thread_id(db)
    task_id = finish_worker_task(thread_id, status)
    job = _jobs(db).one()
    assert job.operation_id == f"{MAIN_AGENT_TASK_TERMINAL}:{task_id}:{status}"
    assert job.payload == {"thread_id": thread_id, "task_id": task_id, "status": status}

    assert _worker().poll(only_types=TERMINAL_JOBS) == 1
    db.expire_all()
    turn = db.query(TurnRecord).one()
    assert turn.source == "runtime_event" and turn.status == "completed"
    inbox = db.query(TurnEventInbox).one()
    assert inbox.status == "handled" and inbox.task_status_read_at is not None
    assert inbox.turn_record_id == turn.id
    assert reviewer.reports[0][0]["report"]["report_usage"]["total_tokens"] == 50
    reply = db.query(Event).filter_by(event_type="llm_response").one()
    assert f"{status} | 真实报告" in reply.payload["content"]
    assert ("证据缺失" in reply.payload["content"]) == (status != "succeeded")

    # Simulate the Outbox losing its completion write after the Turn committed.
    job = _jobs(db).one()
    job.status = "pending"
    db.commit()
    assert _worker().poll(only_types=TERMINAL_JOBS) == 1
    assert len(reviewer.calls) == 2
    assert db.query(Event).filter_by(event_type="llm_response").count() == 1
    assert sum(call.args[1] == "new_message" for call in ws_manager.broadcast_to_thread_sync.call_args_list) == 1


@pytest.mark.parametrize("handle_in_foreground", [True, False])
def test_active_turn_is_reused_at_tool_boundary_or_outbox_recovers(db, reviewer, finish_worker_task, handle_in_foreground):
    thread_id = get_current_thread_id(db)
    task_id = finish_worker_task(thread_id)
    worker = _worker()

    def during_foreground(messages):
        if len(reviewer.calls) == 1:
            assert not _injection(messages)
            assert worker.poll(only_types=TERMINAL_JOBS) == 1
            assert _jobs(db).one().status == "pending"
            assert db.query(TurnRecord).count() == 1
            if not handle_in_foreground:
                return AIMessage(content="原问题已回答")
            return AIMessage(content="", tool_calls=[_tool_call("get_current_time", "clock")])
        assert task_id in _injection(messages)
        return reviewer.review(messages)

    reviewer.respond = during_foreground
    response = TurnExecutionService().execute_turn("请继续原问题", thread_id=thread_id)
    assert "error" not in response
    assert db.query(TurnRecord).filter_by(source="runtime_event").count() == 0
    _make_due(db)
    assert worker.poll(only_types=TERMINAL_JOBS) == 1
    assert db.query(TurnRecord).filter_by(source="runtime_event").count() == (0 if handle_in_foreground else 1)
    assert _jobs(db).one().status == "completed"
    assert len(reviewer.reports) == 1


def test_seven_completed_tasks_share_two_serial_turns(db, reviewer, finish_worker_task):
    thread_id = get_current_thread_id(db)
    task_ids = {finish_worker_task(thread_id, name=f"任务 {i}") for i in range(7)}
    assert _worker().poll(only_types=TERMINAL_JOBS) == 7
    assert db.query(TurnRecord).count() == 2
    assert [len(batch) for batch in reviewer.reports] == [5, 2]
    assert {view["task_id"] for batch in reviewer.reports for view in batch} == task_ids
    assert db.query(TurnEventInbox).filter_by(status="handled").count() == 7


def test_failed_status_read_keeps_event_retryable_and_publishes_no_reply(db, reviewer, finish_worker_task, monkeypatch):
    thread_id = get_current_thread_id(db)
    finish_worker_task(thread_id)
    original = GetTaskStatusTool._run

    @wraps(original)
    def fail_read(*_args, **_kwargs):
        raise ValueError("offline_read_failed")

    def ignores_error(messages):
        if any(isinstance(m, ToolMessage) and "offline_read_failed" in str(m.content) for m in messages):
            return AIMessage(content="误判为已完成")
        return reviewer.review(messages)

    reviewer.respond = ignores_error
    monkeypatch.setattr(GetTaskStatusTool, "_run", fail_read)
    assert _worker().poll(only_types=TERMINAL_JOBS) == 1
    job = _jobs(db).one()
    assert job.status == "pending" and job.retry_count == 1
    row = db.query(TurnEventInbox).one()
    assert row.status == "expired" and row.task_status_read_at is None
    assert db.query(Event).filter_by(event_type="llm_response").count() == 0

    monkeypatch.setattr(GetTaskStatusTool, "_run", original)
    reviewer.respond = reviewer.review
    _make_due(db)
    assert _worker().poll(only_types=TERMINAL_JOBS) == 1
    assert _jobs(db).one().status == "completed"
    assert db.query(Event).filter_by(event_type="llm_response").count() == 1


@pytest.mark.parametrize("independent_message", [False, True])
def test_read_can_end_silently_or_send_one_independent_reminder(db, reviewer, finish_worker_task, independent_message):
    task_id = finish_worker_task(get_current_thread_id(db))

    def decide(messages):
        if len(reviewer.calls) == 1:
            return AIMessage(content="", tool_calls=[_tool_call("notify_user", "too-early", content="未读取")])
        if len(reviewer.calls) == 2:
            assert "read_task_status_before_notify" in str(messages[-2].content)
            return AIMessage(content="", tool_calls=[_tool_call("get_task_status", "read", task_id=task_id)])
        assert "真实报告" in str(messages[-2].content)
        if independent_message:
            return AIMessage(content="", tool_calls=[_tool_call("notify_user", "read-first", content="报告已核验")])
        return AIMessage(content="[[EVENT_DECISION: no_action]]")

    reviewer.respond = decide
    assert _worker().poll(only_types=TERMINAL_JOBS) == 1
    assert _jobs(db).one().status == "completed"
    row = db.query(TurnEventInbox).one()
    assert row.task_status_read_at is not None and row.status == "handled"
    responses = db.query(Event).filter_by(event_type="llm_response").all()
    assert [event.payload["content"] for event in responses] == (["报告已核验"] if independent_message else [])


def test_new_worker_recovers_expired_outbox_and_already_injected_event(db, reviewer, finish_worker_task):
    thread_id = get_current_thread_id(db)
    finish_worker_task(thread_id)
    now = datetime.now(timezone.utc)
    turn = TurnRecord(thread_id=thread_id, turn_id="crashed", turn_sequence=1, status="running",
                      source="user", execution_id="crashed", request_fingerprint="crashed",
                      lease_expires_at=now + timedelta(minutes=5))
    db.add(turn)
    db.commit()
    worker = _worker()
    assert worker.poll(only_types=TERMINAL_JOBS) == 1
    assert not reviewer.calls
    assert claim_pending_events(db, thread_id=thread_id, turn_record_id=turn.id, turn_id=turn.turn_id)
    job = _jobs(db).one()
    turn.lease_expires_at = now - timedelta(seconds=1)
    job.status = "running"
    job.locked_by = "crashed-worker"
    job.claim_token = "expired-claim"
    job.lease_expires_at = now - timedelta(seconds=1)
    job.available_at = None
    db.commit()

    assert _worker().poll(only_types=TERMINAL_JOBS) == 1
    assert _jobs(db).one().status == "completed"
    assert db.query(TurnEventInbox).one().status == "handled"
    assert db.query(Event).filter_by(event_type="llm_response").count() == 1


def test_forgotten_staged_task_cannot_be_injected_or_read(db, reviewer, finish_worker_task):
    thread_id = get_current_thread_id(db)
    task_id = finish_worker_task(thread_id)
    turn = TurnRecord(thread_id=thread_id, turn_id="foreground", turn_sequence=1, status="running",
                      source="user", execution_id="foreground", request_fingerprint="foreground",
                      lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    db.add(turn)
    db.commit()
    worker = _worker()
    assert worker.poll(only_types=TERMINAL_JOBS) == 1
    source = db.query(ContentProvenanceRef).filter_by(owner_type="agent_task", owner_id=task_id).one()
    operation = ForgetOperation(operation_key="offline-forget", mode="redact", selector_type="event",
                                selector_hash="offline", status="shielded")
    db.add(operation)
    db.flush()
    db.add(ForgetTombstone(forget_operation_id=operation.id, target_type=source.source_type,
                           target_id=source.source_id))
    assert not claim_pending_events(db, thread_id=thread_id, turn_record_id=turn.id, turn_id=turn.turn_id)
    db.commit()
    _make_due(db)
    assert worker.poll(only_types=TERMINAL_JOBS) == 1
    assert not reviewer.calls
    assert db.query(TurnEventInbox).one().decision == "source_blocked"
    assert _jobs(db).one().status == "completed"
    from aiive.context.run_context import RunContext
    from aiive.task_runtime.tools import build_main_agent_registry
    from aiive.tools.langchain_adapter import build_langchain_tools

    status_tool = build_langchain_tools(
        build_main_agent_registry(), RunContext(thread_id=thread_id, trace_id="forget-test"), ["get_task_status"],
    )[0]
    outcome = json.loads(status_tool.invoke({"task_id": task_id}))
    assert outcome["ok"] is False and "task_not_found" in outcome["error"]


def test_terminal_transition_and_outbox_share_rollback_and_cancellation_stays_local(db):
    repo = TaskRepository(db)
    thread_id = get_current_thread_id(db)
    task = repo.create_task(thread_id=thread_id, goal="事务测试", enqueue=False)
    task_id = task.id
    repo.start_run(task_id, trigger="test")
    db.commit()
    repo.transition_task(task, TaskStatus.FAILED.value, error="failure")
    assert _jobs(db).count() == 1
    db.rollback()
    assert _jobs(db).count() == 0 and repo.get_task(task_id).status == "running"
    repo.cancel_task(task_id)
    db.commit()
    assert _jobs(db).count() == 0
