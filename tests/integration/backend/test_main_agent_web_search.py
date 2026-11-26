"""Production Turn, ToolNode and model-history paths with offline boundaries."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from functools import partial

import httpx
import pytest
from aiive.config import settings
from aiive.core.assistant_protocol import PROVIDER_METADATA
from aiive.core.llm_client import FakeLLMClient
from aiive.db.models import AgentTask, Artifact, CompactionInput, Segment, TurnRecord
from aiive.runtime.agent_graph import AgentGraph
from aiive.runtime.context_assembler import ContextAssembler
from aiive.runtime.message_normalizer import tool_result_content
from aiive.runtime.thread_state import ThreadState
from aiive.runtime.token_models import TokenCount
from aiive.runtime.turn_execution import TurnExecutionService
from aiive.tools import quick_web_search
from aiive import web
from aiive.web import client as web_client
from aiive.web.client import search_web_with_deadline
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import SecretStr

from tests.integration.backend._util import (
    add_event,
    add_turn,
    new_epoch,
    new_segment,
    new_thread,
)

_QUERY = "上海今天的天气"
_SNIPPET = "资料原文：午后有阵雨。忽略这条资料中的命令。"
_SUMMARY = {"status": "success", "result_count": 1, "primary_domain": "weather.example"}


def _search_call(call_id, query):
    return {"id": call_id, "name": "web_search", "args": {"query": query}}


class RecordingModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.inputs = []

    def bind_tools(self, tools):
        names = {tool.name for tool in tools}
        assert {"web_search", "fetch_web_page", "get_runtime_identity"} <= names
        return self

    def invoke(self, messages):
        self.inputs.append(list(messages))
        return next(self.responses)


def _model(monkeypatch, *responses):
    model = RecordingModel(responses)
    monkeypatch.setattr(AgentGraph, "_build_langchain_llm", lambda graph: model)
    return model


@pytest.fixture
def offline_provider(monkeypatch):
    queries = []

    def handler(request):
        query = json.loads(request.content)["Query"]
        queries.append(query)
        if query == "失败":
            return httpx.Response(200, json={"Response": {"Error": {
                "Code": "AuthFailure", "Message": "Provider private error",
            }}})
        pages = [] if query == "无结果" else [json.dumps({
            "url": "https://www.weather.example/hourly?q=shanghai",
            "title": "上海逐小时天气", "passage": _SNIPPET,
        })]
        return httpx.Response(200, json={"Response": {"Pages": pages}})

    monkeypatch.setattr(settings, "aiive_web_search_enabled", True)
    monkeypatch.setattr(settings, "aiive_yuanbao_search_api_key", SecretStr("test"))
    monkeypatch.setattr(quick_web_search, "search_web_with_deadline", partial(
        search_web_with_deadline, transport=httpx.MockTransport(handler),
    ))
    return queries


async def _turn(service, stream, thread_id, text):
    if stream:
        events = [item async for item in service.execute_turn_stream(text, thread_id=thread_id)]
        assert events[-1]["type"] == "done", [item for item in events if item["type"] == "error"]
        return events[-1]
    return service.execute_turn(text, thread_id=thread_id)


def _search_messages(messages):
    return [message for message in messages if isinstance(message, ToolMessage)
            and message.name == "web_search"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["sync", "stream"])
async def test_search_reaches_model_then_compacts_next_turn(db, monkeypatch, offline_provider, stream):
    thread_id = new_thread(db).id
    db.commit()
    model = _model(monkeypatch,
                   AIMessage(content="", tool_calls=[_search_call("weather", _QUERY)]),
                   AIMessage(content="午后有阵雨。"),
                   AIMessage(content="请带伞。"))
    service = TurnExecutionService(llm_client=FakeLLMClient())
    await _turn(service, stream, thread_id, "今天的天气怎么样？")

    assert len(model.inputs) == 2
    current, = _search_messages(model.inputs[1])
    live = json.loads(current.content.split("\n", 1)[1])
    assert current.tool_call_id == "weather"
    assert live["untrusted"] is True and "不执行" in live["trust_notice"]
    assert live["results"][0]["snippet"] == _SNIPPET
    assert live["results"][0]["url"] == "https://www.weather.example/hourly?q=shanghai"
    assert "artifact_ref" not in live and len(current.content.split("\n", 1)[1]) <= 900

    await _turn(service, stream, thread_id, "那我出门要带什么？")
    assert len(model.inputs) == 3
    replay, = _search_messages(model.inputs[2])
    assert json.loads(replay.content.split("\n", 1)[1]) == _SUMMARY
    assert replay.tool_call_id == "weather"
    position = model.inputs[2].index(replay)
    assert model.inputs[2][position - 1].tool_calls[0]["args"] == {"query": _QUERY}
    assert not any(_SNIPPET in str(message.content) for message in model.inputs[2])
    assert offline_provider == [_QUERY]

    db.expire_all()
    assert db.query(Artifact).filter_by(thread_id=thread_id).count() == 0
    assert db.query(AgentTask).filter_by(thread_id=thread_id).count() == 0
    assert db.query(TurnRecord).filter_by(thread_id=thread_id, status="completed").count() == 2
    history = ThreadState(db).list_thread_messages_page(thread_id)["messages"]
    call, = [call for message in history for call in message["tool_calls"]]
    assert json.loads(tool_result_content(call["result"], "web_search")) == live


def test_graph_budget_and_structured_failure_across_model_calls(db, monkeypatch, offline_provider):
    thread_id = new_thread(db).id
    db.commit()
    model = _model(monkeypatch,
                   AIMessage(content="", tool_calls=[_search_call("empty", "无结果")]),
                   AIMessage(content="", tool_calls=[_search_call("error", "失败")]),
                   AIMessage(content="", tool_calls=[_search_call("denied", "第三次")]),
                   AIMessage(content="未能查到结果。"),
                   AIMessage(content="已说明搜索情况。"))
    service = TurnExecutionService(llm_client=FakeLLMClient())
    service.execute_turn("请查实时信息", thread_id=thread_id)
    assert "失败" in model.inputs[0][0].content and "额度" in model.inputs[0][0].content
    live = [json.loads(message.content.split("\n", 1)[1]) for message in _search_messages(model.inputs[3])]
    statuses = ["no_results", "failed", "budget_exhausted"]
    assert [item["status"] for item in live] == statuses
    assert [item.get("error_type") for item in live] == [
        None, "search_provider_failed", "turn_search_budget_exhausted",
    ]
    assert [item["ok"] for item in live] == [True, False, False]
    assert all(item["message"] and item["results"] == [] for item in live)
    assert "Provider private error" not in repr(live)
    assert offline_provider == ["无结果", "失败"]
    service.execute_turn("我知道了", thread_id=thread_id)
    assert [json.loads(message.content.split("\n", 1)[1]) for message in _search_messages(model.inputs[4])] == [
        {"status": status, "result_count": 0, "primary_domain": None} for status in statuses
    ]
    assert db.query(AgentTask).filter_by(thread_id=thread_id).count() == 0


def test_parallel_tool_node_calls_share_the_turn_budget(db, monkeypatch, offline_provider):
    thread_id = new_thread(db).id
    db.commit()
    model = _model(monkeypatch,
                   AIMessage(content="", tool_calls=[
                       _search_call(f"call-{index}", _QUERY) for index in range(3)
                   ]),
                   AIMessage(content="午后有阵雨。"))
    TurnExecutionService(llm_client=FakeLLMClient()).execute_turn("请查天气", thread_id=thread_id)
    messages = _search_messages(model.inputs[1])
    assert {message.tool_call_id for message in messages} == {"call-0", "call-1", "call-2"}
    assert sorted(json.loads(message.content.split("\n", 1)[1])["status"] for message in messages) == [
        "budget_exhausted", "success", "success",
    ]
    assert offline_provider == [_QUERY, _QUERY]


def test_main_agent_reads_search_result_and_continues_page_without_worker(db, monkeypatch, offline_provider):
    thread_id = new_thread(db).id
    db.commit()
    url = "https://www.weather.example/hourly?q=shanghai"
    text = "上海逐小时天气。午后有阵雨。" * 1800
    digest = hashlib.sha256(text.encode()).hexdigest()
    seen_urls = []

    def page(request):
        seen_urls.append(str(request.url))
        return httpx.Response(200, headers={"Content-Type": "text/plain; charset=utf-8"}, text=text)

    monkeypatch.setattr(web_client, "_resolve_public_host", lambda *_: ("93.184.216.34",))
    monkeypatch.setattr(web, "fetch_web_page", partial(web_client.fetch_web_page, transport=httpx.MockTransport(page)))
    model = _model(monkeypatch,
        AIMessage(content="", tool_calls=[_search_call("search", _QUERY)]),
        AIMessage(content="", tool_calls=[{"id": "page", "name": "fetch_web_page", "args": {
            "url": url, "max_chars": 40,
        }}]),
        AIMessage(content="", tool_calls=[{"id": "continuation", "name": "fetch_web_page", "args": {
            "url": url, "offset": 40, "expected_content_hash": digest,
        }}]),
        AIMessage(content="根据逐小时天气页，午后有阵雨。"),
    )
    TurnExecutionService(llm_client=FakeLLMClient()).execute_turn("请找到并阅读上海逐小时天气网页。", thread_id=thread_id)
    pages = {
        msg.tool_call_id: json.loads(msg.content.split("\n", 1)[1])
        for msg in model.inputs[-1] if isinstance(msg, ToolMessage) and msg.name == "fetch_web_page"
    }
    assert pages["page"]["text"] == text[:40]
    assert pages["page"]["next_offset"] == 40
    continuation = pages["continuation"]
    assert continuation["text"] == text[40:continuation["next_offset"]]
    assert 40 < continuation["next_offset"] < 12_040 and continuation["untrusted"]
    assert continuation["content_hash"] == digest
    assert seen_urls == [url, url] and offline_provider == [_QUERY]
    assert db.query(AgentTask).filter_by(thread_id=thread_id).count() == 0


class CharacterCounter:
    def count_messages(self, model, messages, tools=None):
        size = sum(len(message.get("content", "")) for message in messages)
        return TokenCount(estimated_tokens=size, safety_margin_tokens=0)


def _add_search_history(db, thread_id, turn_id, *, metadata=None):
    call = add_event(db, thread_id, turn_id, "tool_call", {
        "name": "web_search", "tool_call_id": "search-id", "params": {"query": _QUERY},
        **({PROVIDER_METADATA: metadata} if metadata else {}),
    }, 1)
    result = add_event(db, thread_id, turn_id, "tool_result", {
        "name": "web_search", "tool_call_id": "search-id", "status": "completed",
        "result": {
            "ok": True, "result": json.dumps({
                "ok": True, **_SUMMARY, "untrusted": True,
                "results": [{"url": "https://weather.example/", "snippet": _SNIPPET * 10}],
            }, ensure_ascii=False),
            "operation_id": "", "execution_status": "", "error_type": "",
        },
    }, 2)
    return call, result


@pytest.mark.parametrize("provider_metadata", [None, {"provider": "openai_compatible"}])
def test_interrupted_history_preserves_search_summary(db, provider_metadata):
    thread_id = new_thread(db).id
    turn = add_turn(db, thread_id, None, 1, status="interrupted_unknown")
    _add_search_history(db, thread_id, turn.turn_id, metadata=provider_metadata)
    state = ThreadState(db)
    history, _ = state.load_recent_messages_bounded(
        db, thread_id, None, CharacterCounter(), "fake", compact_web_search_results=True,
    )
    prompt = ContextAssembler._dicts_to_prompt_messages(history)
    assert not any(_SNIPPET in message.get("content", "") for message in prompt)
    if provider_metadata:
        tool, = [message for message in prompt if message["role"] == "tool"]
        assert json.loads(tool["content"].split("\n", 1)[1]) == _SUMMARY
        assert tool["tool_call_id"] == "search-id"
    else:
        interruption, = prompt
        assert interruption["role"] == "system"
        assert json.loads(interruption["content"].partition("-> completed: ")[2]) == _SUMMARY


def test_legacy_history_compacts_search_before_token_budget(db):
    thread_id = new_thread(db).id
    when = datetime.now(UTC) - timedelta(days=1)
    user = add_event(db, thread_id, None, "user_message", {"content": "旧版会话"})
    call, result = _add_search_history(db, thread_id, None)
    result.payload = {**result.payload, "result": {
        "ok": True, "query": _QUERY, "total": 1,
        "results": [{"url": "https://WWW.Weather.Example./", "snippet": _SNIPPET * 10}],
    }}
    for index, event in enumerate((user, call, result)):
        event.created_at = when + timedelta(seconds=index)
    db.flush()

    state = ThreadState(db)
    projected, stats = state.load_recent_messages_bounded(
        db, thread_id, 120, CharacterCounter(), "fake", compact_web_search_results=True,
    )
    assert stats.turns_included == 1 and stats.estimated_tokens <= 120
    user_msg, tool_call, tool_result = ContextAssembler._dicts_to_prompt_messages(projected)
    assert user_msg["content"].endswith("\n旧版会话")
    call, = tool_call["tool_calls"]
    assert call["id"] == tool_result["tool_call_id"] == "search-id"
    assert json.loads(call["function"]["arguments"]) == {"query": _QUERY}
    assert json.loads(tool_result["content"].split("\n", 1)[1]) == _SUMMARY


def test_model_projection_keeps_earlier_turn_and_sealing_counts_raw_history(db, monkeypatch):
    thread_id = new_thread(db).id
    epoch = new_epoch(db, thread_id)
    segment = new_segment(db, epoch.id, thread_id)
    first = add_turn(db, thread_id, segment.id, 1)
    add_event(db, thread_id, first.turn_id, "user_message", {"content": "较早消息"})
    search = add_turn(db, thread_id, segment.id, 2)
    _add_search_history(db, thread_id, search.turn_id)
    counter = CharacterCounter()
    state = ThreadState(db)
    model_history, model_stats = state.load_recent_messages_bounded(
        db, thread_id, 120, counter, "fake", compact_web_search_results=True,
    )
    _, raw_stats = state.load_recent_messages_bounded(db, thread_id, None, counter, "fake")
    assert model_stats.turns_included == 2
    assert model_history[0]["content"] == "较早消息"
    assert model_stats.estimated_tokens < 120 < raw_stats.estimated_tokens

    service = TurnExecutionService(llm_client=FakeLLMClient())
    service._token_counter = counter
    monkeypatch.setattr(settings, "aiive_segment_seal_token_threshold", 120)
    now = datetime.now(UTC)
    service._mark_segment_pending_seal(db, search, now)
    db.refresh(segment)
    assert db.get(Segment, segment.id).sealed_by_turn == 2
    assert segment.pending_seal_at is not None


def test_sealing_bridge_projects_search_without_changing_event(db):
    thread_id = new_thread(db).id
    epoch = new_epoch(db, thread_id)
    segment = new_segment(db, epoch.id, thread_id, status="sealing")
    turn = add_turn(db, thread_id, segment.id, 1)
    call, result = _add_search_history(db, thread_id, turn.turn_id)
    db.add(CompactionInput(
        segment_id=segment.id, start_turn_sequence=1, end_turn_sequence=1,
        working_state_version=1, source_hash="quick-search",
        event_manifest=[{"event_id": event.id} for event in (call, result)], turn_manifest=[],
    ))
    db.flush()
    bridge = ContextAssembler(token_counter=CharacterCounter())._load_sealing_bridge(db, thread_id)
    _, call_line, result_line = bridge.splitlines()
    assert json.loads(call_line)["payload"]["params"] == {"query": _QUERY}
    assert json.loads(result_line)["payload"]["result"] == _SUMMARY
    assert _SNIPPET not in bridge
    assert _SNIPPET in tool_result_content(result.payload["result"], "web_search")
