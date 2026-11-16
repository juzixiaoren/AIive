"""Shared Function Calling contract, repair pairing and proposal-only adapters."""
from copy import deepcopy
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, ToolMessage, messages_from_dict
from langchain_core.utils.function_calling import convert_to_openai_tool

from aiive.core.function_calling import (
    build_chat_model, correction_allowed, function_definition, normalize_calls, response_usage, validate_calls,
)
from aiive.core.llm_client import FakeLLMClient
from aiive.task_runtime.function_decisions import WorkerFunctions, proposal_function_name
from aiive.tools.registry import ToolRegistration, CapabilitySafetySchema, ToolRegistry
from aiive.tools.langchain_adapter import build_langchain_tools


def registry(handler=None):
    result = ToolRegistry()
    result.register(ToolRegistration(CapabilitySafetySchema("probe", "local_builtin", "trusted", "low"),
        handler or Mock(), input_schema={"type": "object", "additionalProperties": False,
            "$defs": {"Row": {"type": "object", "additionalProperties": False,
                "required": ["count"], "properties": {"count": {"type": "integer", "minimum": 1}}}},
            "properties": {"rows": {"type": "array", "items": {"$ref": "#/$defs/Row"}}}, "required": ["rows"]}))
    return result


def message(*calls, **kwargs):
    return AIMessage(content="", tool_calls=[{"id": f"call_{index}", "name": name, "args": args}
        for index, (name, args) in enumerate(calls)], **kwargs)


def test_model_factory_explicitly_disables_hidden_infrastructure_retries():
    model = build_chat_model(FakeLLMClient())
    assert model.max_retries == 0
    assert not model.streaming
    assert build_chat_model(FakeLLMClient(), streaming=True).streaming


def test_main_and_worker_derive_one_effective_contract_without_execution():
    handler = Mock()
    registered = registry(handler)
    functions = WorkerFunctions({"available": [registered.definition("probe")]})
    name = proposal_function_name("probe")
    capability = registered.get("probe")
    canonical = function_definition(capability)["parameters"]
    wrapper = functions.registry.get(name).input_schema
    expected = deepcopy(canonical)
    definitions = expected.pop("$defs")
    assert wrapper["properties"]["arguments"] == expected
    assert wrapper["$defs"] == definitions
    assert build_langchain_tools(registered)[0].args_schema == canonical
    convert_to_openai_tool(next(d for d in functions.definitions() if d["function"]["name"] == name))
    call = message((name, {"arguments": {"rows": [{"count": 2}]}, "summary": "propose"}))
    assert validate_calls(call, functions.registry, require_one=True).ok
    proposal = functions.proposal(name, call.tool_calls[0]["args"])
    assert proposal.capability_id == "probe" and proposal.arguments == {"rows": [{"count": 2}]}
    handler.assert_not_called()
    with pytest.raises(RuntimeError, match="not_executable"):
        functions.registry.get(name).handler(arguments={"rows": []})
    assert len(name) <= 64


def test_wrapped_nested_error_and_safe_actual_type():
    functions = WorkerFunctions({"resident": [registry().definition("probe")]})
    call = message((proposal_function_name("probe"), {"arguments": {"rows": [{"count": "SECRET"}]}}))
    result = validate_calls(call, functions.registry, require_one=True)
    issue = result.errors["call_0"]["issues"][0]
    assert issue["path"] == "arguments.rows.0.count"
    assert issue["expected"] == "integer" and issue["received"] == "str"
    assert "SECRET" not in str(result.errors)


@pytest.mark.parametrize("calls", [[], [("task_finish", {"summary": "one"}), ("task_finish", {"summary": "two"})]])
def test_worker_requires_exactly_one_call_and_pairs_all_repair_messages(calls):
    functions = WorkerFunctions({})
    original = message(*calls)
    result = validate_calls(original, functions.registry, require_one=True)
    assert not result.ok
    repair = messages_from_dict(result.retry_turn(original))
    if calls:
        assert {m.tool_call_id for m in repair if isinstance(m, ToolMessage)} == {c["id"] for c in original.tool_calls}
    else:
        assert "tool_call_count" in repair[-1].content


def test_main_may_reply_with_text_and_validate_batch_atomically():
    registered = registry()
    assert validate_calls(AIMessage(content="answer"), registered).ok
    calls = message(("probe", {"rows": [{"count": "bad"}]}), ("probe", {"rows": [{"count": 2}]}))
    result = validate_calls(calls, registered)
    assert len(result.errors) == 2
    assert result.errors["call_1"]["error_type"] == "batch_aborted_due_to_invalid_arguments"


def test_invalid_json_arguments_never_repaired_or_executed():
    malformed = AIMessage(content="", invalid_tool_calls=[{"id": "bad", "name": "probe", "args": '{"secret":', "error": "parser"}])
    normalized = normalize_calls(malformed)
    assert normalized.invalid_tool_calls == []
    assert normalized.tool_calls[0]["args"] == {}
    result = validate_calls(normalized, registry())
    assert result.errors["bad"]["issues"][0]["code"] == "invalid_tool_arguments"
    assert "secret" not in str(result.retry_turn(normalized))


def test_duplicate_ids_rejected_and_repair_pairing_is_unambiguous():
    original = message(("probe", {"rows": []}), ("probe", {"rows": []}))
    original.tool_calls[1]["id"] = "call_0"
    normalized = normalize_calls(original)
    assert len({c["id"] for c in normalized.tool_calls}) == 2
    assert not validate_calls(normalized, registry()).ok


@pytest.mark.parametrize("name,args,decision", [
    ("task_finish", {"summary": "done", "report_status": "partial"}, "finish"),
    ("task_ask_user", {"question": "what?"}, "ask_user"),
    ("task_watch", {"watch": {"watch_type": "time", "config": {"at": "2026-11-17T00:00:00Z"}}}, "watch"),
    ("task_search_capabilities", {"query": "file"}, "search_capabilities"),
    ("task_describe_capability", {"capability_id": "probe"}, "describe_capability"),
])
def test_controls_map_to_internal_dto(name, args, decision):
    functions = WorkerFunctions({})
    assert validate_calls(message((name, args)), functions.registry, require_one=True).ok
    assert functions.proposal(name, args).decision == decision


def test_only_current_loaded_definitions_are_bound():
    definition = registry().definition("probe")
    functions = WorkerFunctions({"loaded": [{**definition, "status": "definition_changed"}]})
    assert not functions.capabilities
    functions = WorkerFunctions({"loaded": [{**definition, "status": "ready"}]})
    assert set(functions.capabilities.values()) == {"probe"}


def test_same_correction_limit_and_usage_for_both_hosts():
    assert correction_allowed(1) and correction_allowed(2) and not correction_allowed(3)
    assert response_usage(AIMessage(content="", usage_metadata={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8})) == {
        "prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    assert response_usage(AIMessage(content="", response_metadata={"token_usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}))["total_tokens"] == 8


def test_main_native_invalid_call_repairs_before_handler():
    from langchain_core.messages import HumanMessage
    from aiive.runtime.agent_graph import AgentGraph
    calls = []
    registered = registry(lambda rows: calls.append(rows) or {"count": len(rows)})
    class Assistant:
        count = 0
        def invoke(self, messages):
            self.count += 1
            if self.count == 1:
                return AIMessage(content="", invalid_tool_calls=[{"id": "broken", "name": "probe", "args": '{"rows":', "error": "parse"}])
            if self.count == 2:
                assert isinstance(messages[-1], ToolMessage)
                assert messages[-1].tool_call_id == "broken"
                return message(("probe", {"rows": [{"count": 1}]}))
            return AIMessage(content="done")
    assistant = Assistant()
    compiled, _, _ = AgentGraph(FakeLLMClient(), Mock())._build_graph(assistant, build_langchain_tools(registered), registered)
    compiled.invoke({"messages": [HumanMessage(content="probe")], "tool_records": []})
    assert calls == [[{"count": 1}]] and assistant.count == 3


def test_main_invalid_high_risk_arguments_repair_before_approval():
    from dataclasses import replace
    from langchain_core.messages import HumanMessage
    from aiive.runtime.agent_graph import AgentGraph
    handler = Mock()
    registered = registry(handler)
    original = registered.get("probe")
    registered.register(replace(original, safety=replace(original.safety, risk_level="high", requires_confirmation=True)))
    class Assistant:
        count = 0
        def invoke(self, messages):
            self.count += 1
            return message(("probe", {"rows": [{"count": "invalid"}]}))
    assistant = Assistant()
    compiled, _, approvals = AgentGraph(FakeLLMClient(), Mock())._build_graph(assistant, build_langchain_tools(registered), registered)
    compiled.invoke({"messages": [HumanMessage(content="probe")], "tool_records": []})
    assert assistant.count == 3 and approvals == []
    handler.assert_not_called()
