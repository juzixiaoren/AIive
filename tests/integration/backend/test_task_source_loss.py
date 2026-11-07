"""Real Worker/Task/Forget path also runs in the default SQLite CI job."""
from unittest.mock import Mock

import pytest

from aiive.db.forget_models import ForgetOperation
from aiive.db.models import AgentTaskEvent, TaskEvidence
from aiive.task_runtime.agent_executor import AgentDecision, TaskAgentExecutor
from aiive.task_runtime.read_repository import TaskContentUnavailable
from aiive.task_runtime.repository import TaskRepository
from aiive.task_runtime.schemas import ActionProposal
from aiive.tools.forget_tool import handle_forget
from aiive.tools.registry import CapabilitySafetySchema, ToolRegistration, ToolRegistry
from aiive.worker import handlers_agent_tasks
from aiive.worker.outbox_dto import HandlerOutcome
from tests.integration.backend.test_forget_v6261 import source, uid
from tests.support import persist_running_job


@pytest.mark.parametrize("phase", ["model", "tool_result"])
def test_source_loss_completes_job_without_backfill_or_retry(db, monkeypatch, phase):
    from aiive.control import capability_broker

    thread, _, _ = source(db)
    thread_id = thread.id
    registry = ToolRegistry()

    def forget():
        result = handle_forget(mode="history_only", thread_id=thread_id, requested_by=uid())
        assert result["ok"]
        return result

    registry.register(ToolRegistration(
        CapabilitySafetySchema(capability_id="test_forget", definition_source="local_builtin",
            definition_trust_level="trusted", risk_level="low",
            allowed_instruction_sources=["task_runtime"], timeout_seconds=2),
        forget, parameters={},
    ))
    monkeypatch.setattr(capability_broker, "build_registry_for_thread", lambda *a: registry)

    def decide(*args, **kwargs):
        if phase == "model":
            forget()
            raise TaskContentUnavailable("task_context_source_changed")
        return AgentDecision(proposal=ActionProposal(decision="action", capability_id="test_forget"),
            model="test", token_usage={}, input_snapshot={}, prompt_refs=[])

    model = Mock(side_effect=decide)
    monkeypatch.setattr(TaskAgentExecutor, "decide", model)
    task = TaskRepository(db).create_task(thread_id=thread_id, goal="forget test",
        task_brief={"scope": {"allowed_capabilities": ["test_forget"]}}, enqueue=False)
    job = persist_running_job(db, job_type="agent_task_run", payload={"task_id": task.id})

    result = handlers_agent_tasks.handle_agent_task_run(job)
    assert result.outcome == HandlerOutcome.COMPLETED
    assert result.reason == "forget_source_blocked:task_content_unavailable"
    assert db.query(ForgetOperation).count() == 1
    assert db.query(TaskEvidence).filter_by(task_id=task.id).count() == 0
    assert db.query(AgentTaskEvent).filter_by(task_id=task.id, event_type="run_failed").count() == 0

    # Redelivery must converge before either the model or the tool runs again.
    second = handlers_agent_tasks.handle_agent_task_run(job)
    assert second.outcome == HandlerOutcome.COMPLETED
    assert second.reason.startswith("forget_source_blocked")
    assert model.call_count == 1
    assert db.query(ForgetOperation).count() == 1
