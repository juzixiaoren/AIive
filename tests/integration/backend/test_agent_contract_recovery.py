"""Canonical contracts and real Outbox handler recovery (offline LLM boundary)."""
from datetime import timedelta
from types import SimpleNamespace
import uuid

import pytest

from aiive.db.models import AgentAction, AgentRun, AgentTask, AgentTaskEvent, Thread
from aiive.task_runtime.agent_executor import TaskAgentExecutor
from aiive.task_runtime.errors import AgentOutputValidationError
from aiive.task_runtime.repository import TaskRepository, utcnow
from aiive.task_runtime.schemas import TaskBudget
from aiive.task_runtime.tools import build_main_agent_registry
from aiive.tools.langchain_adapter import build_langchain_tools
from aiive.tools.registry import ToolRegistry, ToolRegistration, CapabilitySafetySchema
from aiive.tools.tool_validation import canonical_tool_schema, validate_tool_params
from aiive.worker.handlers_agent_tasks import handle_agent_task_run
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome
from tests.support import ensure_running_claim


@pytest.fixture
def durable_db(db_session):
    from sqlalchemy.orm import Session
    from tests.support import set_current_test_session
    session = Session(db_session.get_bind().engine)
    set_current_test_session(session)
    try:
        yield session
    finally:
        session.close()
        set_current_test_session(db_session)


def test_delegate_budget_contract():
    registry = build_main_agent_registry()
    reg = registry.get("delegate_task")
    schema = canonical_tool_schema(reg)
    expected = {**TaskBudget.model_json_schema(), "type": ["object", "null"], "default": None}
    assert schema["properties"]["budgets"] == expected
    assert validate_tool_params(reg, {"goal": "x", "budgets": None}).ok
    tool = next(t for t in build_langchain_tools(registry) if t.name == "delegate_task")
    from langchain_core.utils.function_calling import convert_to_openai_tool
    def without_titles(value):
        if isinstance(value, dict):
            return {key: without_titles(child) for key, child in value.items() if key != "title"}
        if isinstance(value, list):
            return [without_titles(child) for child in value]
        return value
    assert convert_to_openai_tool(tool)["function"]["parameters"]["properties"]["budgets"] == without_titles(expected)
    assert tool.args_schema["properties"]["budgets"] == expected
    invalid = validate_tool_params(reg, {"goal": "x", "budgets": {"max_steps": 3}})
    assert not invalid.ok
    assert invalid.issues[0].path == "budgets.max_steps"
    assert validate_tool_params(reg, {"goal": "x", "budgets": {"max_actions": 3}}).ok
    assert not validate_tool_params(reg, {"goal": "x", "budgets": {"max_actions": "3"}}).ok


@pytest.mark.parametrize("source", ["local_builtin", "remote_mcp", "desktop_node"])
def test_explicit_nested_contract_is_authoritative(source):
    schema = {"type": "object", "required": ["items"], "additionalProperties": False,
              "properties": {"items": {"type": "array", "items": {"type": "object",
                  "required": ["count", "mode"], "additionalProperties": False,
                  "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 3, "default": 2},
                                 "mode": {"type": "string", "enum": ["a", "b"], "minLength": 1, "maxLength": 2}}}}}}
    reg = ToolRegistration(CapabilitySafetySchema("nested", source, "trusted", "low"),
                           lambda **kwargs: kwargs, parameters={"wrong": "str"}, input_schema=schema)
    registry = ToolRegistry()
    registry.register(reg)
    assert registry.definition("nested")["input_schema"] == schema
    assert build_langchain_tools(registry)[0].args_schema == schema
    assert validate_tool_params(reg, {"items": [{"count": 2, "mode": "a"}]}).ok
    for args, path in [({}, "items"), ({"items": [{"count": 0, "mode": "a"}]}, "items.0.count"),
                       ({"items": [{"count": 2, "mode": "c"}]}, "items.0.mode")]:
        result = validate_tool_params(reg, args)
        assert not result.ok and result.issues[0].path == path
        assert result.issues[0].expected
    old = registry.get("nested").safety.descriptor_hash
    schema["properties"]["items"]["items"]["properties"]["count"]["maximum"] = 4
    registry.register(reg)
    assert registry.get("nested").safety.descriptor_hash != old


def setup_job(db):
    thread = Thread(id=str(uuid.uuid4()), title="contract recovery")
    db.add(thread)
    db.flush()
    task = TaskRepository(db).create_task(thread_id=thread.id, goal="test", enqueue=False,
        task_brief={"scope": {"allowed_capabilities": ["get_current_time"]}})
    claimed = ClaimedJob(str(uuid.uuid4()), "agent_task_run", {"task_id": task.id}, None, 0, 3, "claim", 1, "test")
    ensure_running_claim(claimed, db=db)
    return task.id, claimed


class Model:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.messages = []

    def chat(self, messages, **kwargs):
        self.messages.append(messages)
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        assert "ActionProposal JSON Schema" in messages[0]["content"]
        return SimpleNamespace(content=output, model="offline", usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


def wire_model(monkeypatch, outputs):
    model = Model(outputs)
    monkeypatch.setattr("aiive.task_runtime.agent_executor.default_llm_client", lambda: model)
    return model


@pytest.mark.parametrize("limit", [1, 2, 30])
def test_structural_exhaustion_real_handler(durable_db, monkeypatch, limit):
    task_id, claimed = setup_job(durable_db)
    task = durable_db.get(AgentTask, task_id)
    task.budgets = {"max_model_calls": limit}
    durable_db.commit()
    model = wire_model(monkeypatch, ['{"decision": "bogus"}'] * 3)
    result = handle_agent_task_run(claimed)
    assert result.outcome == HandlerOutcome.COMPLETED
    durable_db.expire_all()
    task = durable_db.get(AgentTask, task_id)
    run = durable_db.get(AgentRun, task.current_run_id)
    calls = min(limit, 3)
    assert task.status == "failed" and run.status == "failed" and run.lease_expires_at is None
    assert task.usage["model_calls"] == calls == len(model.messages)
    assert task.usage["prompt_tokens"] == calls * 10
    assert run.token_usage["total_tokens"] == calls * 15
    assert durable_db.query(AgentAction).filter_by(task_id=task_id).count() == 0
    if calls > 1:
        assert "agent_output_validation_error" in model.messages[1][-1]["content"]


def test_structure_then_arguments_corrected_before_action(durable_db, monkeypatch):
    task_id, claimed = setup_job(durable_db)
    model = wire_model(monkeypatch, ['{"decision":"bad"}',
        '{"decision":"action","capability_id":"get_current_time","arguments":{"max_steps":2}}',
        '{"decision":"action","capability_id":"get_current_time"}',
        '{"decision":"finish","summary":"done"}'])
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
    durable_db.expire_all()
    assert durable_db.get(AgentTask, task_id).status == "succeeded"
    actions = durable_db.query(AgentAction).filter_by(task_id=task_id).all()
    assert len(actions) == 1 and actions[0].status == "succeeded"
    assert "max_steps" in model.messages[2][-1]["content"]
    assert durable_db.get(AgentTask, task_id).usage["model_calls"] == 4


@pytest.mark.parametrize("error", [TimeoutError("timeout"), RuntimeError("rate_limit"), ValueError("unexpected")])
def test_infrastructure_recovery_real_handler(durable_db, monkeypatch, error):
    task_id, claimed = setup_job(durable_db)
    wire_model(monkeypatch, [error])
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.RETRYABLE_ERROR
    durable_db.expire_all()
    task = durable_db.get(AgentTask, task_id)
    assert task.status == "queued"
    run = durable_db.get(AgentRun, task.current_run_id)
    assert run.status == "failed" and run.lease_expires_at is None
    assert durable_db.query(AgentTaskEvent).filter_by(task_id=task_id, event_type="run_failed").count() == 1
    wire_model(monkeypatch, ['{"decision":"finish"}'])
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
    durable_db.expire_all()
    assert durable_db.get(AgentTask, task_id).status == "succeeded"


@pytest.mark.parametrize("expired", [False, True])
def test_duplicate_running_real_handler(durable_db, monkeypatch, expired):
    task_id, claimed = setup_job(durable_db)
    task, run = TaskRepository(durable_db).start_run(task_id, trigger="test")
    if expired:
        run.lease_expires_at = utcnow() - timedelta(seconds=1)
    durable_db.commit()
    model = wire_model(monkeypatch, [])
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
    durable_db.expire_all()
    assert durable_db.get(AgentTask, task_id).status == ("queued" if expired else "running")
    assert model.messages == []


def test_parse_strict_and_repair():
    assert TaskAgentExecutor.parse('{"decision":"finish",}').decision == "finish"
    with pytest.raises(AgentOutputValidationError) as caught:
        TaskAgentExecutor.parse('{"decision":"finish", "summary": 1}')
    assert caught.value.issues[0]["path"] == "summary"
    assert caught.value.issues[0]["received"] == "int"


def test_bad_parameters_exhaust_without_action(durable_db, monkeypatch):
    task_id, claimed = setup_job(durable_db)
    model = wire_model(monkeypatch, ['{"decision":"action","capability_id":"get_current_time","arguments":{"max_steps":2}}'] * 3)
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
    durable_db.expire_all()
    assert durable_db.get(AgentTask, task_id).status == "failed"
    assert durable_db.query(AgentAction).filter_by(task_id=task_id).count() == 0
    assert len(model.messages) == 3
    assert "max_steps" in model.messages[1][-1]["content"]


def test_exhaustion_after_progress_is_partial(durable_db, monkeypatch):
    task_id, claimed = setup_job(durable_db)
    wire_model(monkeypatch, ['{"decision":"action","capability_id":"get_current_time"}'] + ['{"decision":"bad"}'] * 3)
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
    durable_db.expire_all()
    task = durable_db.get(AgentTask, task_id)
    assert task.status == "partial"
    assert durable_db.get(AgentRun, task.current_run_id).status == "failed"
    assert task.usage["model_calls"] == 4


def test_unknown_effect_recovery_does_not_replay(durable_db, monkeypatch):
    task_id, claimed = setup_job(durable_db)
    def interrupted_decision(self, task, capabilities):
        repo = TaskRepository(self.db)
        run = self.db.get(AgentRun, task.current_run_id)
        action = repo.create_action(task=task, run=run, capability_id="get_current_time",
            arguments={}, descriptor_hash="test", risk_level="low", requires_approval=False)
        for status in ["validated", "ready", "dispatched"]:
            repo.transition_action(action, status)
        self.db.commit()
        raise RuntimeError("execution_unknown")
    monkeypatch.setattr(TaskAgentExecutor, "decide", interrupted_decision)
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.RETRYABLE_ERROR
    durable_db.expire_all()
    task = durable_db.get(AgentTask, task_id)
    assert task.status == "reconciling"
    assert durable_db.query(AgentAction).filter_by(task_id=task_id).one().status == "unknown"
    assert durable_db.get(AgentRun, task.current_run_id).status == "failed"
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
    assert durable_db.query(AgentAction).filter_by(task_id=task_id).count() == 1


def test_correction_cannot_expand_scope(durable_db, monkeypatch):
    task_id, claimed = setup_job(durable_db)
    wire_model(monkeypatch, [
        '{"decision":"action","capability_id":"list_tasks","arguments":{"bad":1}}',
        '{"decision":"action","capability_id":"list_tasks"}',
        '{"decision":"finish","report_status":"failed"}',
    ])
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
    durable_db.expire_all()
    actions = durable_db.query(AgentAction).filter_by(task_id=task_id).all()
    assert len(actions) == 1 and actions[0].status == "failed"
    assert actions[0].error_message == "unknown_capability"  # restricted registry hides out-of-scope tools


def test_validation_tokens_survive_following_timeout(durable_db, monkeypatch):
    task_id, claimed = setup_job(durable_db)
    wire_model(monkeypatch, ['{"decision":"bad"}', TimeoutError("timeout")])
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.RETRYABLE_ERROR
    durable_db.expire_all()
    task = durable_db.get(AgentTask, task_id)
    run = durable_db.get(AgentRun, task.current_run_id)
    assert task.status == "queued" and run.status == "failed"
    assert task.usage["model_calls"] == 2
    assert task.usage["prompt_tokens"] == 10
    assert run.token_usage["total_tokens"] == 15


def test_old_worker_failure_cannot_fail_new_owner(durable_db, monkeypatch):
    task_id, claimed = setup_job(durable_db)
    def replaced_decision(self, task, capabilities):
        repo = TaskRepository(self.db)
        old = self.db.get(AgentRun, task.current_run_id)
        repo.finish_run(old, status="interrupted")
        repo.transition_task(task, "queued")
        repo.start_run(task.id, trigger="new_owner")
        self.db.commit()
        raise TimeoutError("old_worker_timed_out")
    monkeypatch.setattr(TaskAgentExecutor, "decide", replaced_decision)
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.RETRYABLE_ERROR
    durable_db.expire_all()
    task = durable_db.get(AgentTask, task_id)
    current = durable_db.get(AgentRun, task.current_run_id)
    assert task.status == "running" and current.status == "running"
    assert current.lease_expires_at is not None
    assert durable_db.query(AgentRun).filter_by(task_id=task_id, status="failed").count() == 0


def test_tool_budget_never_truncates_schema():
    from aiive.task_runtime.tool_injection import build_tool_view
    registry = ToolRegistry()
    registry.register(ToolRegistration(CapabilitySafetySchema("large", "remote_mcp", "trusted", "low"),
        lambda **kwargs: kwargs, input_schema={"type": "object", "properties": {
            "text": {"type": "string", "description": "x" * 30000}}, "additionalProperties": False}))
    view = build_tool_view(registry, [registry.definition("large")], {})
    assert view["mode"] == "discovery"
    assert view["resident"] == [] and view["available"] == []
    assert view["omitted_capability_ids"] == ["large"]
    assert registry.definition("large")["input_schema"]["properties"]["text"]["description"] == "x" * 30000


def test_infrastructure_retry_does_not_reset_corrections(durable_db, monkeypatch):
    task_id, claimed = setup_job(durable_db)
    wire_model(monkeypatch, ['{"decision":"bad"}', TimeoutError("timeout")])
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.RETRYABLE_ERROR
    model = wire_model(monkeypatch, ['{"decision":"bad"}'] * 3)
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
    durable_db.expire_all()
    task = durable_db.get(AgentTask, task_id)
    assert task.status == "failed"
    assert task.usage["model_calls"] == 4
    assert len(model.messages) == 2
