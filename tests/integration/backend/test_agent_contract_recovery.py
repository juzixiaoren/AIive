"""Canonical contracts and real Outbox handler recovery (offline LLM boundary)."""

from aiive.tools.base import BaseTool, DatabaseTool
from datetime import timedelta
import json
from langchain_core.messages import AIMessage
from aiive.task_runtime.function_decisions import WorkerFunctions, proposal_function_name
from aiive.core.function_calling import normalize_calls, validate_calls
import uuid

import pytest

from aiive.db.models import AgentAction, AgentRun, AgentTask, AgentTaskEvent, TaskEvidence, Thread
from aiive.task_runtime.agent_executor import TaskAgentExecutor
from aiive.task_runtime.repository import TaskRepository, utcnow
from aiive.task_runtime.schemas import TaskBudget
from aiive.task_runtime.tools import build_main_agent_registry
from aiive.tools.langchain_adapter import build_langchain_tools
from aiive.tools.registry import ToolRegistry, ToolDefinition, CapabilitySafetySchema
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
    class FakeTool1(BaseTool):

        def _run(self, **kwargs):
            return kwargs
    reg = FakeTool1(ToolDefinition(CapabilitySafetySchema('nested', source, 'trusted', 'low'), parameters={'wrong': 'str'}, input_schema=schema))
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
        self.bound_names = []

    def bind_tools(self, definitions, **kwargs):
        from aiive.runtime.context_budget import ContextBudget

        self.definitions = definitions
        self.bound_names.append({item["function"]["name"] for item in definitions})
        assert kwargs == {"tool_choice": "auto", "parallel_tool_calls": False,
                          "max_tokens": ContextBudget.default().reserved_output_tokens}
        return self

    def invoke(self, messages, **kwargs):
        self.messages.append(messages)
        if kwargs.get("config", {}).get("run_name") == "task_report":
            return AIMessage(content="执行结果与待完成事项。")
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        assert "ActionProposal JSON Schema" not in messages[0].content
        assert "input_schema" not in messages[1].content
        if isinstance(output, AIMessage):
            return output
        # Existing recovery scenarios now exercise native function calls. JSON
        # fixture notation is converted only in this test boundary, never production.
        decision = json.loads(output)
        kind = decision.pop("decision")
        if kind == "action":
            name = proposal_function_name(decision.pop("capability_id"))
            decision.setdefault("arguments", {})
        else:
            name = {"finish": "task_finish", "ask_user": "task_ask_user", "watch": "task_watch",
                    "search_capabilities": "task_search_capabilities", "describe_capability": "task_describe_capability"}.get(kind, "task_unknown")
            if kind == "finish":
                decision.setdefault("summary", "")
        return AIMessage(content="", tool_calls=[{"id": "decision_call", "name": name, "args": decision}],
                         response_metadata={"model_name": "offline"},
                         usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})


def wire_model(monkeypatch, outputs):
    model = Model(outputs)
    monkeypatch.setattr("aiive.task_runtime.agent_executor.build_chat_model", lambda _: model)
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
    assert task.status == "partial"
    assert run.status == "completed" and run.lease_expires_at is None
    assert task.report["stop_reason"] == ("model_call_budget_exhausted" if limit <= 2 else "no_progress")
    assert task.usage["model_calls"] == calls
    assert len(model.messages) == calls + int(limit <= 2)
    assert task.usage["prompt_tokens"] == calls * 10
    assert run.token_usage["total_tokens"] == calls * 15
    assert durable_db.query(AgentAction).filter_by(task_id=task_id).count() == 0
    if calls > 1:
        assert "agent_output_validation_error" in model.messages[1][-1].content


def test_structure_then_arguments_corrected_before_action(durable_db, monkeypatch):
    task_id, claimed = setup_job(durable_db)
    model = wire_model(monkeypatch, ['{"decision":"bad"}',
        '{"decision":"action","capability_id":"get_current_time","arguments":{"max_steps":2}}',
        '{"decision":"action","capability_id":"get_current_time"}',
        '{"decision":"finish","summary":"done"}'])
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
    durable_db.expire_all()
    task = durable_db.get(AgentTask, task_id)
    assert task.status == "succeeded"
    assert task.report["summary"] == "执行结果与待完成事项。"
    actions = durable_db.query(AgentAction).filter_by(task_id=task_id).all()
    assert len(actions) == 1 and actions[0].status == "succeeded"
    assert "max_steps" in model.messages[2][-1].content
    assert task.usage["model_calls"] == 4 and len(model.messages) == 5


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


def test_function_arguments_strict_and_invalid_json_not_repaired():
    functions = WorkerFunctions({})
    message = normalize_calls(AIMessage(content="", tool_calls=[{
        "id": "strict", "name": "task_finish", "args": {"summary": 1},
    }]))
    validation = validate_calls(message, functions.registry, require_one=True)
    assert not validation.ok
    assert validation.errors["strict"]["issues"][0]["path"] == "summary"
    assert validation.errors["strict"]["issues"][0]["received"] == "int"
    malformed = normalize_calls(AIMessage(content="", invalid_tool_calls=[{
        "id": "bad-json", "name": "task_finish", "args": '{"summary":', "error": "invalid json",
    }]))
    assert not validate_calls(malformed, functions.registry, require_one=True).ok


def test_bad_parameters_exhaust_without_action(durable_db, monkeypatch):
    task_id, claimed = setup_job(durable_db)
    model = wire_model(monkeypatch, ['{"decision":"action","capability_id":"get_current_time","arguments":{"max_steps":2}}'] * 3)
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
    durable_db.expire_all()
    task = durable_db.get(AgentTask, task_id)
    assert task.status == "partial" and task.report["stop_reason"] == "no_progress"
    assert durable_db.query(AgentAction).filter_by(task_id=task_id).count() == 0
    assert len(model.messages) == 3
    assert "max_steps" in model.messages[1][-1].content


def test_exhaustion_after_progress_is_partial(durable_db, monkeypatch):
    task_id, claimed = setup_job(durable_db)
    wire_model(monkeypatch, ['{"decision":"action","capability_id":"get_current_time"}'] + ['{"decision":"bad"}'] * 3)
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
    durable_db.expire_all()
    task = durable_db.get(AgentTask, task_id)
    assert task.status == "partial"
    assert durable_db.get(AgentRun, task.current_run_id).status == "completed"
    assert task.report["stop_reason"] == "no_progress"
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
    model = wire_model(monkeypatch, [
        '{"decision":"action","capability_id":"list_reminders","arguments":{"bad":1}}',
        '{"decision":"action","capability_id":"list_reminders"}',
        '{"decision":"finish","report_status":"failed"}',
    ])
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
    durable_db.expire_all()
    actions = durable_db.query(AgentAction).filter_by(task_id=task_id).all()
    assert actions == []
    assert all(proposal_function_name("list_reminders") not in names for names in model.bound_names)


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
    class FakeTool2(BaseTool):

        def _run(self, **kwargs):
            return kwargs
    registry.register(FakeTool2(ToolDefinition(CapabilitySafetySchema('large', 'remote_mcp', 'trusted', 'low'), input_schema={'type': 'object', 'properties': {'text': {'type': 'string', 'description': 'x' * 30000}}, 'additionalProperties': False})))
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
    assert task.status == "partial" and task.report["stop_reason"] == "no_progress"
    assert task.usage["model_calls"] == 4
    assert len(model.messages) == 2


@pytest.mark.parametrize("kind", ["text", "empty", "multi", "malformed", "unknown"])
def test_native_fc_protocol_errors_exhaust_without_actions(durable_db, monkeypatch, kind):
    task_id, claimed = setup_job(durable_db)
    kwargs = {"content": "plain answer" if kind == "text" else ""}
    if kind == "multi":
        kwargs["tool_calls"] = [{"id": f"call{i}", "name": "task_finish", "args": {"summary": "done"}} for i in range(2)]
    if kind == "malformed":
        kwargs["invalid_tool_calls"] = [{"id": "broken", "name": "task_finish", "args": '{"summary":', "error": "parse"}]
    if kind == "unknown":
        kwargs["tool_calls"] = [{"id": "unknown", "name": "not_bound", "args": {}}]
    kwargs["usage_metadata"] = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    model = wire_model(monkeypatch, [AIMessage(**kwargs)] * 3)
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
    durable_db.expire_all()
    task = durable_db.get(AgentTask, task_id)
    assert task.status == "partial" and task.usage["model_calls"] == 3
    assert task.report["stop_reason"] == "no_progress"
    assert task.usage["prompt_tokens"] == 30
    assert durable_db.get(AgentRun, task.current_run_id).token_usage["total_tokens"] == 45
    assert durable_db.query(AgentAction).filter_by(task_id=task_id).count() == 0
    assert len(model.messages) == 3
    assert "agent_output_validation_error" in model.messages[1][-1].content


def test_native_fc_invalid_arguments_repair_messages_are_paired(durable_db, monkeypatch):
    from langchain_core.messages import ToolMessage
    task_id, claimed = setup_job(durable_db)
    malformed = AIMessage(content="", invalid_tool_calls=[{"id": "original_bad_id", "name": "task_finish",
        "args": '{"summary":', "error": "parser"}], usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    model = wire_model(monkeypatch, [malformed, '{"decision":"finish","summary":"fixed"}'])
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
    repair = model.messages[1]
    assert isinstance(repair[-1], ToolMessage) and repair[-1].tool_call_id == "original_bad_id"
    assert repair[-2].tool_calls[0]["id"] == "original_bad_id"
    assert durable_db.get(AgentTask, task_id).status == "succeeded"


def test_discovery_via_control_functions_creates_no_actions(durable_db, monkeypatch):
    task_id, claimed = setup_job(durable_db)
    wire_model(monkeypatch, [
        '{"decision":"search_capabilities","query":"current time"}',
        '{"decision":"describe_capability","capability_id":"get_current_time"}',
        '{"decision":"finish","summary":"discovered"}',
    ])
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
    durable_db.expire_all()
    task = durable_db.get(AgentTask, task_id)
    assert task.status == "succeeded" and task.usage["discovery_steps"] == 2
    assert durable_db.query(AgentAction).filter_by(task_id=task_id).count() == 0


@pytest.fixture
def worker_discovery(monkeypatch):
    from aiive.tool_discovery.dense import DenseIndex, ModelIdentity
    from aiive.tool_discovery.service import DiscoveryService

    class Provider:
        fail = False

        def embed(self, text, *, input_type="document"):
            return [1, 0] if any(term in text for term in ("报销", "invoice", "spreadsheet")) else [0, 1]

        def embed_batch(self, texts, *, batch_size):
            if self.fail:
                raise RuntimeError("offline mock outage")
            return [self.embed(text) for text in texts]

    provider = Provider()
    index = DenseIndex(ModelIdentity("local", "http://embedding/v1", "mock", "r1", 2, ""),
                       lambda: provider, batch_size=4)
    service = DiscoveryService(index)
    monkeypatch.setattr("aiive.control.capability_broker.get_discovery_service", lambda: service)
    monkeypatch.setattr("aiive.tool_discovery.service.get_discovery_service", lambda: service)
    try:
        yield service, provider
    finally:
        index.close()


def _wait_for_discovery(service, registry, status):
    from time import monotonic, sleep

    deadline = monotonic() + 5
    while monotonic() < deadline:
        if service.search(registry, "报销")["status"] == status:
            return
        sleep(0.01)
    pytest.fail(f"Tool discovery did not reach {status}")


def test_worker_semantic_search_binds_batch_and_broker_creates_evidence(durable_db, monkeypatch, worker_discovery):
    from aiive.tool_discovery.documents import DiscoveryMetadata
    from aiive.tools.registry import get_tool_registry

    class DocumentTool(BaseTool):
        def _run(self, **params):
            return {"records": 2, "format": params["format"]}

    registry = get_tool_registry()
    reader, exporter = "test_semantic_reader", "test_semantic_export"
    try:
        for key, description, namespace in (
            (reader, "Read invoice pages", "test:pdf"),
            (exporter, "Extract a spreadsheet", "test:tables"),
        ):
            registry.register(DocumentTool(ToolDefinition(
                CapabilitySafetySchema(key, "local_builtin", "trusted", "low", effect_mode="pure"),
                description=description,
                parameters={"format": {"type": "str", "required": True}},
                discovery=DiscoveryMetadata(namespace_id=namespace),
            )))
        _wait_for_discovery(worker_discovery[0], registry, "ready")
        task_id, claimed = setup_job(durable_db)
        task = durable_db.get(AgentTask, task_id)
        task.task_brief = {"scope": {"capability_policy": "unified"}}
        durable_db.commit()
        model = wire_model(monkeypatch, [
            json.dumps({"decision": "search_capabilities", "query": "整理报销金额", "limit": 5}, ensure_ascii=False),
            json.dumps({"decision": "action", "capability_id": exporter, "arguments": {"format": "csv"}}),
            '{"decision":"finish","summary":"extracted two records"}',
        ])
        assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
        durable_db.expire_all()
        task = durable_db.get(AgentTask, task_id)
        assert task.status == "succeeded"
        expected = {proposal_function_name(reader), proposal_function_name(exporter)}
        assert not expected & model.bound_names[0]
        assert expected <= model.bound_names[1]
        search = durable_db.query(AgentTaskEvent).filter_by(task_id=task_id, event_type="tool_search").one().payload
        assert search["mode"] == "hybrid" and search["status"] == "ready"
        assert set(search["matched"]) == set(search["loaded"]) == {reader, exporter}
        assert set(search["matched_namespace_ids"]) == {"test:pdf", "test:tables"}
        saved = task.task_state["tool_discovery"]["loaded"]
        assert {item["capability_id"] for item in saved} == {reader, exporter}
        assert all(set(item) == {"capability_id", "descriptor_hash"} for item in saved)
        action = durable_db.query(AgentAction).filter_by(task_id=task_id).one()
        assert action.status == "succeeded" and action.capability_id == exporter
        evidence = durable_db.query(TaskEvidence).filter_by(task_id=task_id, action_id=action.id).one()
        assert "records" in evidence.summary and "csv" in evidence.summary
    finally:
        registry.unregister(reader)
        registry.unregister(exporter)


def test_worker_resident_stays_bound_during_embedding_failure(durable_db, monkeypatch, worker_discovery):
    from sqlalchemy.orm import Session

    from aiive.tools.registry import get_tool_registry

    service, provider = worker_discovery
    provider.fail = True
    _wait_for_discovery(service, get_tool_registry(), "embedding_unavailable")
    task_id, claimed = setup_job(durable_db)
    task = durable_db.get(AgentTask, task_id)
    task.task_brief = {"scope": {"allowed_capabilities": ["list_reminders"]}}
    durable_db.commit()
    # The database tool owns a separate session, as it does in production.
    monkeypatch.setattr("aiive.tools.builtin_tools.SessionLocal", lambda: Session(durable_db.get_bind()))
    model = wire_model(monkeypatch, [
        '{"decision":"search_capabilities","query":"list_reminders"}',
        '{"decision":"action","capability_id":"list_reminders"}',
        '{"decision":"finish","summary":"listed reminders"}',
    ])
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
    durable_db.expire_all()
    task = durable_db.get(AgentTask, task_id)
    assert task.status == "succeeded"
    assert proposal_function_name("list_reminders") in model.bound_names[0]
    assert durable_db.query(AgentAction).filter_by(task_id=task_id).one().status == "succeeded"


def test_worker_refuses_definition_changed_during_model_call(durable_db, monkeypatch):
    from dataclasses import replace

    from aiive.tools.registry import get_tool_registry

    task_id, claimed = setup_job(durable_db)
    registry = get_tool_registry()
    original = registry.get("get_current_time")
    model = wire_model(monkeypatch, [
        '{"decision":"action","capability_id":"get_current_time"}',
        '{"decision":"finish","report_status":"partial","summary":"tool changed"}',
    ])
    invoke = model.invoke

    def invoke_during_update(messages, **kwargs):
        response = invoke(messages, **kwargs)
        if any(call["name"] == proposal_function_name("get_current_time") for call in response.tool_calls):
            registry.register(original.with_definition(replace(original.definition, description="Updated clock contract")))
        return response

    monkeypatch.setattr(model, "invoke", invoke_during_update)
    try:
        assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
        action = durable_db.query(AgentAction).filter_by(task_id=task_id).one()
        assert action.status == "failed" and action.error_message == "descriptor_changed"
        assert action.descriptor_hash == original.safety.descriptor_hash
        assert durable_db.query(TaskEvidence).filter_by(task_id=task_id, action_id=action.id).count() == 0
    finally:
        registry.register(original)


def test_model_boundary_preserves_content_unavailable(durable_db, monkeypatch):
    from aiive.task_runtime.read_repository import TaskContentUnavailable
    task_id, claimed = setup_job(durable_db)
    wire_model(monkeypatch, [TaskContentUnavailable("task_context_source_changed")])
    result = handle_agent_task_run(claimed)
    assert result.outcome == HandlerOutcome.COMPLETED
    assert "task_content_unavailable" in result.reason
    assert durable_db.query(AgentAction).filter_by(task_id=task_id).count() == 0


def _cancel_during_run(self, task, capabilities):
    """模拟：运行中用户取消，Worker 在工具执行期间崩溃。"""
    repo = TaskRepository(self.db)
    run = self.db.get(AgentRun, task.current_run_id)
    action = repo.create_action(
        task=task, run=run, capability_id="get_current_time", arguments={},
        descriptor_hash="test", risk_level="low", requires_approval=False,
    )
    for status in ["validated", "ready", "dispatched"]:
        repo.transition_action(action, status)
    repo.cancel_task(task.id, reason="user_cancelled")
    self.db.commit()
    raise RuntimeError("worker crashed after cancel")


def test_cancelled_recovery_completes_outbox_job(durable_db, monkeypatch):
    """取消终态收敛并提交后，本次投递已经产生实际效果，不应再重试。"""
    task_id, claimed = setup_job(durable_db)
    monkeypatch.setattr(TaskAgentExecutor, "decide", _cancel_during_run)

    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED

    durable_db.expire_all()
    task = durable_db.get(AgentTask, task_id)
    assert task.status == "cancelled"
    assert durable_db.get(AgentRun, task.current_run_id).status == "interrupted"
    actions = durable_db.query(AgentAction).filter_by(task_id=task_id).all()
    assert [action.status for action in actions] == ["unknown"]
    assert durable_db.query(AgentTaskEvent).filter_by(
        task_id=task_id, event_type="cancelled_execution_recovered",
    ).count() == 1

    # 再次投递只会读到同一个取消终态，不会重复记录恢复事件。
    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.COMPLETED
    durable_db.expire_all()
    assert durable_db.query(AgentTaskEvent).filter_by(
        task_id=task_id, event_type="cancelled_execution_recovered",
    ).count() == 1


def test_cancelled_recovery_failure_stays_retryable(durable_db, monkeypatch):
    """恢复事务失败时不得报告成功：状态全部回滚，Outbox 仍可重试。"""
    task_id, claimed = setup_job(durable_db)
    monkeypatch.setattr(TaskAgentExecutor, "decide", _cancel_during_run)

    original_checkpoint = TaskRepository.checkpoint

    def failing_checkpoint(self, task_id, *, reason, **kwargs):
        if reason == "cancelled_execution_recovery":
            raise RuntimeError("recovery flush failed")
        return original_checkpoint(self, task_id, reason=reason, **kwargs)

    monkeypatch.setattr(TaskRepository, "checkpoint", failing_checkpoint)

    assert handle_agent_task_run(claimed).outcome == HandlerOutcome.RETRYABLE_ERROR

    durable_db.expire_all()
    assert durable_db.get(AgentTask, task_id).status == "cancelled"
    assert durable_db.query(AgentAction).filter_by(task_id=task_id).one().status == "dispatched"
    assert durable_db.query(AgentTaskEvent).filter_by(
        task_id=task_id, event_type="cancelled_execution_recovered",
    ).count() == 0
