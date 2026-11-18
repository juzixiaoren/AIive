from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from aiive.context.run_context import RunContext
from aiive.core.scheduling_time import schedule_instant
from aiive.db.models import AgentAction, AgentRun, AgentTaskWatch, Event, Task, Thread
from aiive.task_runtime.evidence import EvidenceStore
from aiive.task_runtime.manual_reconciliation import resolve_unknown_action
from aiive.task_runtime.repository import TaskRepository, utcnow
from aiive.task_runtime.runtime import PersistentTaskRuntime
from aiive.task_runtime.watchers import WatcherService
from aiive.task_runtime.work_read_model import WorkReadModel
from aiive.tools.registry import get_tool_registry
from tests.tool_helpers import tool_method


def _task(db, **kwargs):
    thread = Thread(id=str(uuid4()), title="Review")
    db.add(thread)
    db.flush()
    return TaskRepository(db).create_task(thread_id=thread.id, goal="Review", enqueue=False, **kwargs)


def test_invalid_watch_is_rejected_or_retired_without_stalling(db_session):
    task = _task(db_session)
    service = WatcherService(db_session)
    with pytest.raises(ValueError):
        service.create_from_proposal(task, {"watch_type": "time", "config": {}})
    now = utcnow()
    legacy = AgentTaskWatch(task_id=task.id, watch_type="time", config={}, status="active",
                            next_check_at=now, cursor={})
    db_session.add(legacy)
    TaskRepository(db_session).transition_task(task, "blocked_user")
    assert service.poll_due(now=now) == 0
    assert legacy.status == "completed"
    assert legacy.cursor["error"] == "watch_invalid_at"
    assert task.status == "queued"
    valid = service.create_from_proposal(task, {
        "watch_type": "time", "config": {"at": (now + timedelta(hours=1)).isoformat()},
    })
    assert valid.next_check_at == now + timedelta(hours=1)


def test_reverse_history_starts_next_to_the_current_page(db_session):
    task = _task(db_session)
    repo = TaskRepository(db_session)
    for _ in range(130):
        repo.append_event(task.id, "watch_triggered", {"snapshot": "not projected"})
        repo.append_event(task.id, "agent_decision", {}, visibility="internal")
    model = WorkReadModel(db_session)
    initial = model.work_view(task)
    older = model.timeline_history(task.id, initial["history_cursor"])
    latest = initial["timeline"]
    assert len(latest) == len(older["items"]) == 50
    assert older["items"][-1]["sequence"] + 2 == latest[0]["sequence"]
    assert older["items"][-1]["event_type"] == "watch_triggered"
    assert "not projected" not in older["items"][-1]["summary"]


def test_run_limit_creates_no_extra_run(db_session):
    task = _task(db_session, budgets={"max_runs": 1})
    task.usage = {**task.usage, "runs": 1}
    result = PersistentTaskRuntime(db_session).run(task.id)
    assert result["report"]["unresolved"] == ["run_budget_exhausted"]
    assert task.usage["runs"] == 1
    assert db_session.query(AgentRun).filter_by(task_id=task.id).count() == 0


def test_shell_log_survives_temporary_file_and_respects_task_boundary(db_session, tmp_path, monkeypatch):
    from aiive.runtime import tool_output_store
    from aiive.storage import object_store
    from aiive.tools.task_file_tools import ReadTaskArtifactTool

    monkeypatch.setattr(tool_output_store, "tool_output_root", lambda: tmp_path)
    monkeypatch.setattr(object_store, "ROOT", tmp_path / "objects")
    logfile = tmp_path / "stdout.log"
    logfile.write_text("完整输出" * 100, encoding="utf-8")
    task = _task(db_session)
    repo = TaskRepository(db_session)
    _, run = repo.start_run(task.id, trigger="test")
    action = repo.create_action(task=task, run=run, capability_id="run_shell", arguments={},
                                descriptor_hash="test", risk_level="low", requires_approval=False)
    EvidenceStore().capture_result(db_session, task_id=task.id, run_id=run.id, action=action,
                                   result={"ok": True, "result": {"stdout_log": str(logfile)}})
    artifact_id = action.result_summary["output_artifacts"]["stdout"]["artifact_id"]
    logfile.unlink()
    reader = tool_method(ReadTaskArtifactTool)
    ctx = RunContext(task.thread_id, task.id, task_id=task.id)
    first = reader(db_session, ctx, artifact_id, max_bytes=16)
    second = reader(db_session, ctx, artifact_id, offset=first["next_offset"], max_bytes=16)
    assert (first["content"] + second["content"]) == ("完整输出" * 3)[:10]
    other = _task(db_session)
    assert not reader(db_session, RunContext(other.thread_id, other.id, task_id=other.id), artifact_id)["ok"]


def test_user_reconciliation_resumes_once_and_records_evidence(db_session):
    task = _task(db_session)
    repo = TaskRepository(db_session)
    _, run = repo.start_run(task.id, trigger="test")
    action = repo.create_action(task=task, run=run, capability_id="run_shell", arguments={},
                                descriptor_hash="test", risk_level="low", requires_approval=False)
    for status in ("validated", "ready", "dispatched", "unknown"):
        repo.transition_action(action, status)
    repo.transition_task(task, "reconciling")
    with pytest.raises(ValueError, match="action_attempt_changed"):
        resolve_unknown_action(db_session, task.id, action.id, "completed", "Stale confirmation", "previous-attempt")
    assert resolve_unknown_action(db_session, task.id, action.id, "completed", "Observed intended result", action.id)["status"] == "succeeded"
    assert task.status == "queued"
    with pytest.raises(ValueError, match="action_no_longer_unknown"):
        resolve_unknown_action(db_session, task.id, action.id, "completed", "Duplicate confirmation", action.id)
    assert db_session.get(AgentAction, action.id).evidence_refs


def test_batch_dismiss_cancels_old_reminders_beyond_two_hundred_newer_events(db_session):
    from aiive.tools.builtin_reminders import DismissNotificationsTool, ScheduleReminderTool

    task = _task(db_session)
    ctx = RunContext(task.thread_id, task.id, user_timezone="Asia/Shanghai")
    reminder = tool_method(ScheduleReminderTool)(db_session, ctx, "a future reminder", delay_minutes=5)
    db_session.add_all([
        Event(event_type="notification_created", trace_id=str(uuid4()), thread_id=ctx.thread_id,
              payload={"status": "confirmed"})
        for _ in range(201)
    ])
    db_session.flush()
    result = tool_method(DismissNotificationsTool)(db_session, ctx)
    assert result["dismissed_count"] == 1
    assert db_session.get(Task, reminder["task_id"]).status == "cancelled"


def test_absolute_schedule_uses_user_zone_and_dst_is_explicit():
    assert schedule_instant("2026-11-19T09:00", "Asia/Shanghai") == datetime(2026, 11, 19, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="ambiguous_or_nonexistent"):
        schedule_instant("2026-11-01T01:30", "America/New_York")
    with pytest.raises(ValueError, match="requires_timezone"):
        schedule_instant("2026-11-19T09:00")


def test_task_mcp_installation_needs_approval():
    from aiive.control.scope import TaskScope
    from aiive.control.task_policy import TaskPolicyEngine

    scope = TaskScope.from_brief({"scope": {"capability_policy": "unified", "allow_network": True}})
    tool = get_tool_registry().get("install_mcp_sandbox")
    verdict = TaskPolicyEngine().decide(tool, scope, "install_mcp_sandbox", {"candidate_name": "catalog"})
    assert verdict.outcome == "approval"


def test_worker_rejects_a_request_that_cannot_fit_before_contacting_model(db_session, monkeypatch):
    from unittest.mock import Mock

    from aiive.config import settings
    from aiive.runtime.token_models import TokenCount
    from aiive.task_runtime.agent_executor import TaskAgentExecutor
    from aiive.task_runtime.errors import TaskModelBudgetError

    task = _task(db_session)
    monkeypatch.setattr(settings, "aiive_llm_context_window", 4096)
    monkeypatch.setattr(settings, "aiive_llm_max_output_tokens", 1024)
    model = Mock()
    executor = TaskAgentExecutor(db_session, chat_model=model)
    size = Mock(return_value=TokenCount(3500, 500))
    monkeypatch.setattr(executor.counter, "count_messages", size)
    with pytest.raises(TaskModelBudgetError, match="worker_context_budget_exceeded"):
        executor.decide(task, [])
    model.bind_tools.assert_not_called()
    assert size.call_args.args[2]


def test_worker_cost_budget_reserves_and_settles_before_next_call(db_session, monkeypatch):
    from aiive.config import settings
    from aiive.task_runtime.agent_executor import _reserve_model_cost, _settle_model_cost
    from aiive.task_runtime.errors import TaskModelBudgetError

    task = _task(db_session, budgets={"max_cost_usd": 0.01})
    monkeypatch.setattr(settings, "aiive_worker_input_usd_per_million", 1.0)
    monkeypatch.setattr(settings, "aiive_worker_output_usd_per_million", 3.0)
    reservation = _reserve_model_cost(task, 1000, 1000)
    assert task.usage["cost_reserved_usd"] == pytest.approx(0.004)
    _settle_model_cost(task, reservation, {"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600})
    assert task.usage["cost_usd"] == pytest.approx(0.0008)
    assert task.usage["cost_reserved_usd"] == 0
    with pytest.raises(TaskModelBudgetError, match="cost_budget_exhausted"):
        _reserve_model_cost(task, 1000, 4000)


def test_document_pages_are_bound_to_source_version(tmp_path, monkeypatch):
    from aiive.config import settings
    from aiive.tools.builtin_knowledge import ReadDocumentTool

    monkeypatch.setattr(settings, "aiive_knowledge_roots", str(tmp_path))
    filename = tmp_path / "document.txt"
    filename.write_text("first-second-third", encoding="utf-8")
    read = tool_method(ReadDocumentTool)
    page = read(str(filename), max_chars=6)
    second = read(str(filename), max_chars=6, offset=page["next_offset"], version=page["version"])
    assert page["text"] + second["text"] == "first-second"
    filename.write_text("replaced source", encoding="utf-8")
    with pytest.raises(ValueError, match="document_version_changed"):
        read(str(filename), offset=page["next_offset"], version=page["version"])
