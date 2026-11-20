"""Offline DeepSeek wire, graph, persistence and privacy regression tests."""

import asyncio
import json
from collections import deque
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_from_dict
from langchain_openai import ChatOpenAI

from aiive.api import routes_chat
from aiive.api.routes_debug import list_events
from aiive.api.routes_inspector import inspector_events
from aiive.api.routes_threads import get_thread_messages
from aiive.api.ws_manager import ws_manager
from aiive.core import function_calling
from aiive.core.action_planner import MemorySignalDecision
from aiive.core.assistant_protocol import LEGACY_FALLBACK
from aiive.core.deepseek_chat import AIiveChatDeepSeek
from aiive.core.llm_client import LLMClient
from aiive.db.forget_models import ForgetOperation, ForgetShield
from aiive.db.models import AgentTask, Event, LLMCall, OutboxJob, Thread, TurnRecord
from aiive.runtime.agent_graph import AgentGraph
from aiive.runtime.context_assembler import ContextAssembler
from aiive.runtime.conversation_state import get_current_thread_id
from aiive.runtime.thread_state import ThreadState
from aiive.runtime.token_counter import LiteLLMTokenCounter
from aiive.runtime.turn_execution import TurnExecutionService
from aiive.task_runtime.agent_executor import TaskAgentExecutor
from aiive.task_runtime.errors import AgentOutputValidationError
from aiive.tools.base import BaseTool
from aiive.tools.registry import CapabilitySafetySchema, ToolDefinition, ToolRegistry
from aiive.worker.outbox_handlers import _reconstruct_source_turns


USAGE = {"prompt_tokens": 9, "completion_tokens": 6, "total_tokens": 15}
TOOL = {"type": "function", "function": {"name": "probe", "parameters": {"type": "object"}}}
PRIVATE = "internal-reasoning-"


def call(id_, name="probe", arguments="{}"):
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": arguments}}


def reply(reasoning=None, *, text=None, calls=()):
    return {"role": "assistant", "content": text, "reasoning_content": reasoning, "tool_calls": list(calls)}


@pytest.fixture
def wire(monkeypatch):
    responses, requests = deque(), []
    transport_client = LLMClient("https://api.deepseek.com/v1", "mock-key", "deepseek-v4-flash")

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/v1/chat/completions"
        message = responses.popleft()
        if message is None:
            return httpx.Response(400, json={"error": {"message": "mock failure"}})
        finish = "tool_calls" if message["tool_calls"] else "stop"
        header = {"id": "mock", "created": 1, "model": transport_client.default_model}
        if not body.get("stream"):
            return httpx.Response(200, json={
                **header, "object": "chat.completion", "usage": USAGE,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            })

        def chunk(delta, finish_reason=None):
            return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}

        chunks = [chunk({"role": "assistant"})]
        reasoning = message["reasoning_content"]
        if reasoning:
            chunks.extend(chunk({"reasoning_content": part}) for part in (reasoning[:7], reasoning[7:]))
        if message["content"]:
            chunks.append(chunk({"content": message["content"]}))
        if message["tool_calls"]:
            chunks.append(chunk({"tool_calls": [
                {"index": i, **call} for i, call in enumerate(message["tool_calls"])
            ]}))
        chunks.extend([chunk({}, finish), {"choices": [], "usage": USAGE}])
        stream = "".join(
            "data: " + json.dumps({**header, "object": "chat.completion.chunk", **part}) + "\n\n"
            for part in chunks
        ) + "data: [DONE]\n\n"
        return httpx.Response(200, text=stream, headers={"Content-Type": "text/event-stream"})

    transport = httpx.MockTransport(handle)
    async_http = httpx.AsyncClient(transport=transport)
    with httpx.Client(transport=transport) as sync:
        monkeypatch.setattr(function_calling, "AIiveChatDeepSeek", lambda **kwargs: AIiveChatDeepSeek(
            http_client=sync, http_async_client=async_http, **kwargs,
        ))
        yield transport_client, responses, requests
    asyncio.run(async_http.aclose())


def assistants(wire_body):
    return [msg for msg in wire_body["messages"] if msg["role"] == "assistant"]


def test_sync_protocol_and_legacy_fallback(wire):
    client, responses, requests = wire
    model = function_calling.build_chat_model(client)
    assert isinstance(model, AIiveChatDeepSeek)
    assert model.max_retries == 0
    bound = model.bind_tools([TOOL], tool_choice="auto")
    user = HumanMessage(content="Start")
    old = AIMessage(content="Old assistant")
    responses.extend([
        reply(calls=[call("fallback-probe")]), reply(text="Fallback"),
        reply(PRIVATE + "enabled", text="Thinking"),
    ])

    fallback = bound.invoke([user, old, user])
    assert fallback.response_metadata["thinking_mode"] == LEGACY_FALLBACK
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert requests[0]["tool_choice"] == "auto" and requests[0]["tools"]
    assert "reasoning_content" not in assistants(requests[0])[0]
    disabled = bound.invoke([user, old, user, fallback, ToolMessage(
        content="Tool result", tool_call_id="fallback-probe",
    )])
    assert requests[1]["thinking"] == {"type": "disabled"}
    assert disabled.response_metadata["thinking_mode"] == LEGACY_FALLBACK
    recovered = bound.invoke([user, disabled, user])
    assert requests[2]["thinking"] == {"type": "enabled"}
    assert recovered.additional_kwargs["reasoning_content"] == PRIVATE + "enabled"
    assert recovered.response_metadata["thinking_mode"] == "enabled"
    assert recovered.usage_metadata["total_tokens"] == USAGE["total_tokens"]


def test_provider_selection_does_not_change_compatible_chat():
    proxy = LLMClient("https://proxy.test/v1", "mock-key", "deepseek-v4-flash", provider="deepseek")
    assert isinstance(function_calling.build_chat_model(proxy), AIiveChatDeepSeek)
    for url, provider in (("https://api.deepseek.com.evil.test/v1", "auto"),
                          ("https://api.deepseek.com/v1", "openai_compatible")):
        client = LLMClient(url, "mock-key", "deepseek-v4-flash", provider=provider)
        assert type(function_calling.build_chat_model(client)) is ChatOpenAI


@pytest.mark.asyncio
async def test_async_stream_keeps_reasoning_separate_from_answer(wire):
    client, responses, requests = wire
    responses.extend([reply(PRIVATE + "prior", text="First"), reply(PRIVATE + "async", text="Next")])
    model = function_calling.build_chat_model(client).bind_tools([TOOL])
    prior = await model.ainvoke([HumanMessage(content="First?")])
    chunks = [chunk async for chunk in model.astream([prior, HumanMessage(content="Next?")])]
    merged = chunks[0]
    for chunk in chunks[1:]:
        merged += chunk
    assert merged.content == "Next"
    assert merged.additional_kwargs["reasoning_content"] == PRIVATE + "async"
    assert merged.response_metadata["thinking_mode"] == "enabled"
    assert merged.usage_metadata["total_tokens"] == USAGE["total_tokens"]
    assert assistants(requests[1])[0]["reasoning_content"] == PRIVATE + "prior"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
async def test_chat_memory_write_and_fallback_are_shared_by_sync_and_stream(db, wire, monkeypatch, streaming, explicit):
    from aiive.db.models import MemoryProposal, MemoryRecord, ToolOperation
    from aiive.tools import operation_executor
    from aiive.worker.handler_registry import HandlerRegistry
    from aiive.worker.outbox_handlers import register_all
    from aiive.worker.outbox_heartbeat import ActiveClaimRegistry
    from aiive.worker.outbox_worker import OutboxWorker

    client, responses, requests = wire
    args = json.dumps({"content": "dark theme", "memory_type": "user_profile", "memory_key": "user.preference.theme"})
    if explicit:
        responses.append(reply(PRIVATE + "write", calls=[call("remember", "remember_or_update", args)]))
    responses.append(reply(PRIVATE + "final", text="Done"))

    classified = []

    def classify(*args):
        classified.append(args)
        return MemorySignalDecision(action="extract_async", confidence=1, reason="test fallback")

    handlers = HandlerRegistry()
    register_all(handlers)
    worker = OutboxWorker("memory-chat-test", handlers, ActiveClaimRegistry())
    wait = operation_executor.wait_for_tool_operation

    def dispatch_and_wait(operation_id, _timeout):
        assert worker.poll(max_jobs=1, only_types=frozenset({"tool_operation"})) == 1
        return wait(operation_id, 0)

    monkeypatch.setattr(AgentGraph, "_classify_memory_signal", classify)
    monkeypatch.setattr(operation_executor, "wait_for_tool_operation", dispatch_and_wait)
    service = TurnExecutionService(client)
    thread_id = get_current_thread_id(db)
    message = "I prefer a dark theme."
    if streaming:
        events = [event async for event in service.execute_turn_stream(message, thread_id, "memory-chat")]
        assert events[-1]["type"] == "done"
    else:
        result = await asyncio.to_thread(service.execute_turn, message, thread_id, "memory-chat")
        assert result["reply"] == "Done"

    turn = db.query(TurnRecord).filter_by(turn_id="memory-chat").one()
    if explicit:
        assert not classified
        assert db.query(OutboxJob).filter_by(job_type="memory_extraction").count() == 0
        receipt = db.query(ToolOperation).filter_by(turn_record_id=turn.id).one()
        assert receipt.status == "committed" and receipt.result_payload["state"] == "active"
        assert db.query(MemoryRecord).filter_by(id=receipt.result_payload["memory_id"]).count() == 1
        assert db.query(MemoryProposal).filter_by(source_turn_record_id=turn.id).one().source_event_ids == [turn.request_event_id]
        tool_message = next(msg for msg in requests[-1]["messages"] if msg["role"] == "tool")
        assert json.loads(tool_message["content"])["written"] is True
    else:
        assert len(classified) == 1
        job = db.query(OutboxJob).filter_by(job_type="memory_extraction").one()
        assert turn.request_event_id in job.payload["source_event_ids"]


@pytest.mark.asyncio
async def test_private_metadata_survives_streamed_batches_and_a_new_turn(db, wire, monkeypatch):
    client, responses, requests = wire
    responses.extend([
        reply(PRIVATE + "parallel", calls=[call("left"), call("right")]),
        reply(PRIVATE + "next batch", calls=[call("next")]),
        reply(PRIVATE + "final", text="Visible answer"),
        reply(PRIVATE + "second turn", text="Visible follow-up"),
    ])
    invocations = _probe(monkeypatch)
    monkeypatch.setattr(routes_chat, "default_llm_client", lambda: client)
    monkeypatch.setattr(AgentGraph, "_classify_memory_signal", lambda *args: MemorySignalDecision(
        action="extract_async", confidence=1, reason="test",
    ))
    monkeypatch.setattr(ws_manager, "_main_loop", asyncio.get_running_loop())
    socket = AsyncMock()
    thread_id = get_current_thread_id(db)
    ws_manager.attach(thread_id, socket)
    try:
        response = await routes_chat.chat_stream(routes_chat.ChatRequest(
            thread_id=thread_id, turn_id="first", message="Run probes",
        ))
        sse = "".join([part async for part in response.body_iterator])
    finally:
        ws_manager.disconnect(thread_id, socket)
    assert "event: done" in sse and "event: error" not in sse
    assert PRIVATE not in sse and "reasoning_content" not in sse
    assert socket.send_text.await_args_list
    assert PRIVATE not in str(socket.send_text.await_args_list)
    assert len(invocations) == 3
    assert [m["reasoning_content"] for m in assistants(requests[2])] == [
        PRIVATE + "parallel", PRIVATE + "next batch",
    ]

    first = db.query(Event).filter(
        Event.turn_id == "first", Event.event_type.in_(("tool_call", "tool_result", "llm_response")),
    ).order_by(Event.turn_event_index).all()
    assert [e.event_type for e in first] == [
        "tool_call", "tool_call", "tool_result", "tool_result", "tool_call", "tool_result", "llm_response",
    ]
    assert [e.payload["provider_metadata"]["reasoning_content"] for e in first if "provider_metadata" in e.payload] == [
        PRIVATE + "parallel", PRIVATE + "next batch", PRIVATE + "final",
    ]
    memory_job = db.query(OutboxJob).filter_by(job_type="memory_extraction").one()
    assert memory_job.payload["reply"] == "Visible answer"
    assert PRIVATE not in str(memory_job.payload)
    for output in (
        get_thread_messages(thread_id, db=db),
        list_events(thread_id=thread_id, trace_id=None, db=db),
        inspector_events(thread_id=thread_id, trace_id=None, limit=50, db=db),
        _reconstruct_source_turns(db, [{"event_id": e.id} for e in first]),
    ):
        assert PRIVATE not in str(output) and "reasoning_content" not in str(output)

    service = TurnExecutionService(client)
    followup = await asyncio.to_thread(service.execute_turn, "Follow up", thread_id, "second")
    assert followup["reply"] == "Visible follow-up"
    assert [m["reasoning_content"] for m in assistants(requests[3])] == [
        PRIVATE + "parallel", PRIVATE + "next batch", PRIVATE + "final",
    ]
    assert [c["id"] for c in assistants(requests[3])[0]["tool_calls"]] == ["left", "right"]
    assert [m["tool_call_id"] for m in requests[3]["messages"] if m["role"] == "tool"] == [
        "left", "right", "next",
    ]
    await asyncio.to_thread(service.execute_turn, "Follow up", thread_id, "second")
    assert len(requests) == 4 and len(invocations) == 3

    _shield_event(db, first[0])
    history, _ = ThreadState(db).load_recent_messages_bounded(
        db, thread_id, None, LiteLLMTokenCounter(), "deepseek/deepseek-v4-flash",
    )
    messages = ContextAssembler._dicts_to_prompt_messages(history)
    assert PRIVATE + "parallel" not in str(messages)
    assert PRIVATE + "next batch" in str(messages)
    assert "left" not in [m.get("tool_call_id") for m in messages]


def _probe(monkeypatch, *, requires_confirmation=False):
    calls = []

    class Probe(BaseTool):
        def _run(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "result": "probe result"}

    registry = ToolRegistry()
    registry.register(Probe(ToolDefinition(
        CapabilitySafetySchema("probe", "local_builtin", "trusted", "low",
                               requires_confirmation=requires_confirmation),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )))
    monkeypatch.setattr("aiive.runtime.turn_tool_surface.build_turn_tool_registry", lambda _: registry)
    return calls


def _shield_event(db, event):
    operation = ForgetOperation(
        mode="everywhere", selector_type="explicit_ids", operation_key="forget-reasoning",
        selector_hash="test", status="shielded",
    )
    db.add(operation)
    db.flush()
    db.add(ForgetShield(
        forget_operation_id=operation.id, selector_type="explicit_ids",
        target_type="event", target_id=event.id, status="active", normalized_shield_key="forget-reasoning",
        cutoff_created_at=datetime.now(timezone.utc),
    ))
    db.commit()


@pytest.mark.parametrize("approval", [False, True])
def test_unexecuted_batch_retains_metadata_without_a_public_tool_result(db, wire, monkeypatch, approval):
    client, responses, requests = wire
    name = "probe" if approval else "nonexistent_tool"
    responses.extend([reply(PRIVATE + "policy", calls=[call("policy-call", name)]),
                      reply(PRIVATE + "followup", text="Complete")])
    invocations = _probe(monkeypatch, requires_confirmation=approval)
    monkeypatch.setattr(AgentGraph, "_classify_memory_signal", lambda *args: MemorySignalDecision(
        action="skip", confidence=1, reason="test",
    ))
    service = TurnExecutionService(client)
    thread_id = get_current_thread_id(db)
    first = service.execute_turn("Check policy", thread_id, "policy")
    assert first["reply"] and not invocations
    batch = db.query(Event).filter(
        Event.turn_id == "policy", Event.event_type.in_(("tool_call", "tool_result")),
    ).order_by(Event.turn_event_index).all()
    assert len(batch) == 2 and all(e.payload["protocol_only"] for e in batch)
    assert batch[0].payload["provider_metadata"]["reasoning_content"] == PRIVATE + "policy"
    assert not any(message["tool_calls"] for message in get_thread_messages(thread_id, db=db)["messages"]
                   if message["role"] == "assistant")
    if approval:
        # Model history resolves the receipt emitted by the approval path into the
        # original Assistant call, including when approval was handled after the turn.
        for index, kind, fields in (
            (10, "tool_call", {"params": {}}),
            (11, "tool_result", {"result": "approved receipt", "status": "completed"}),
        ):
            db.add(Event(thread_id=thread_id, turn_id="policy", trace_id="approval",
                         turn_event_index=index, event_type=kind, payload={
                             "name": name, "tool_call_id": "policy-call", "approval_id": "approval",
                             **fields,
                         }))
        db.commit()
    result = service.execute_turn("Continue", thread_id, "policy-followup")
    assert result["reply"] == "Complete"
    assert requests[1]["thinking"] == {"type": "enabled"}
    assert assistants(requests[1])[0]["reasoning_content"] == PRIVATE + "policy"
    results = [m for m in requests[1]["messages"] if m["role"] == "tool"]
    assert len(results) == 1 and results[0]["tool_call_id"] == "policy-call"
    if approval:
        assert results[0]["content"] == "approved receipt"


@pytest.mark.asyncio
async def test_interrupted_stream_replays_confirmed_tools_and_reasoning(db, wire, monkeypatch):
    client, responses, requests = wire
    responses.extend([reply(PRIVATE + "interrupted", calls=[call("completed-probe")]), None,
                      reply(PRIVATE + "resumed", text="Resumed")])
    invocations = _probe(monkeypatch)
    monkeypatch.setattr(AgentGraph, "_classify_memory_signal", lambda *args: MemorySignalDecision(
        action="skip", confidence=1, reason="test",
    ))
    service = TurnExecutionService(client)
    thread_id = get_current_thread_id(db)
    events = [event async for event in service.execute_turn_stream("Run probe", thread_id, "interrupted")]
    assert events[-1]["type"] == "error" and len(invocations) == 1
    assert db.query(TurnRecord).filter_by(turn_id="interrupted").one().status == "interrupted_unknown"
    resumed = await asyncio.to_thread(service.execute_turn, "Continue", thread_id, "resumed")
    assert resumed["reply"] == "Resumed" and len(invocations) == 1
    assert assistants(requests[2])[0]["reasoning_content"] == PRIVATE + "interrupted"
    assert [m["tool_call_id"] for m in requests[2]["messages"] if m["role"] == "tool"] == ["completed-probe"]


def test_worker_repairs_malformed_arguments_with_original_protocol_fields(db, wire):
    client, responses, requests = wire
    responses.extend([
        reply(PRIVATE + "worker", calls=[call("broken", "task_finish", '{"private_argument":')]),
        reply(PRIVATE + "worker repaired", calls=[call(
            "fixed", "task_finish", '{"summary":"Deliverable","report_status":"succeeded"}',
        )]),
    ])
    thread = Thread(id="worker")
    task = AgentTask(thread_id=thread.id, title="Test", goal="Write a deliverable")
    db.add(thread)
    db.flush()
    db.add(task)
    db.commit()
    executor = TaskAgentExecutor(db, client)
    with pytest.raises(AgentOutputValidationError) as caught:
        executor.decide(task, [])
    repair = caught.value.feedback
    assistant = messages_from_dict(repair)[0]
    assert assistant.additional_kwargs == {"reasoning_content": PRIVATE + "worker"}
    assert assistant.response_metadata == {"provider": "deepseek", "thinking_mode": "enabled"}
    assert "private_argument" not in str(repair)
    task.task_state = {"validation_messages": repair}
    db.commit()

    decision = executor.decide(task, [])
    assert decision.proposal.decision == "finish"
    assert requests[1]["thinking"] == {"type": "enabled"}
    assert requests[1]["tool_choice"] == "auto"
    assert requests[1]["parallel_tool_calls"] is False
    assert assistants(requests[1])[-1]["reasoning_content"] == PRIVATE + "worker"
    assert PRIVATE not in str(decision.input_snapshot)
    assert requests[1]["messages"][-1]["tool_call_id"] == "broken"


def test_legacy_provenance_and_token_budget(db):
    db.add(Thread(id="legacy"))
    db.flush()
    for index, model in enumerate(("deepseek/gpt-4o", "deepseek/deepseek-reasoner", "private-model"), 1):
        turn_id = "turn-" + str(index)
        db.add(TurnRecord(thread_id="legacy", turn_id=turn_id, turn_sequence=index, status="completed"))
        db.add(Event(thread_id="legacy", trace_id=turn_id, turn_id=turn_id, turn_event_index=1,
                     event_type="llm_response", payload={"content": model}))
        db.add(LLMCall(thread_id="legacy", trace_id=turn_id, model=model, latency_ms=0))
    db.commit()
    model = "deepseek/deepseek-v4-flash"
    counter = LiteLLMTokenCounter()
    events, _ = ThreadState(db).load_recent_messages_bounded(db, "legacy", None, counter, model)
    messages = ContextAssembler._dicts_to_prompt_messages(events)
    assert messages[0]["provider_metadata"] == {"provider": "openai_compatible"}
    assert "provider_metadata" not in messages[1] and "provider_metadata" not in messages[2]
    plain = [{"role": "assistant", "content": "Short reply"}]
    with_reasoning = [{**plain[0], "provider_metadata": {
        "provider": "deepseek", "reasoning_content": "Think about constraints. " * 200,
    }}]
    assert counter.count_messages(model, with_reasoning).estimated_tokens > counter.count_messages(model, plain).estimated_tokens + 200
