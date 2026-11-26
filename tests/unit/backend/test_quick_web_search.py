"""Main Agent search budgets, provider contract and cancellation."""

import asyncio
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from threading import Barrier, Event

import httpx
import pytest
from aiive.config import settings
from aiive.context.run_context import MAX_WEB_SEARCH_CALLS_PER_TURN, RunContext
from aiive.task_runtime.tools import build_main_agent_registry
from aiive.tools import executor
from aiive.tools import quick_web_search as quick
from aiive.tools.builtin_web import FetchWebPageTool, WebSearchTool
from aiive.tools.registry import get_tool_registry
from aiive.web import client as web_client
from pydantic import SecretStr


@pytest.fixture
def search(monkeypatch):
    monkeypatch.setattr(settings, "aiive_web_search_enabled", True)
    monkeypatch.setattr(settings, "aiive_yuanbao_search_api_key", SecretStr("test-secret"))
    registry = build_main_agent_registry()

    def execute(ctx, query="今日天气"):
        return registry.execute("web_search", {"query": query}, "trusted_user_command", ctx)

    return registry, execute


def _mock_transport(monkeypatch, handler):
    monkeypatch.setattr(quick, "search_web_with_deadline", partial(
        web_client.search_web_with_deadline, transport=httpx.MockTransport(handler),
    ))


def _response(*pages):
    return httpx.Response(200, json={"Response": {"Pages": [json.dumps(page) for page in pages]}})


def test_main_and_worker_registrations(search):
    main, _ = search
    worker = get_tool_registry()
    quick_tool = main.get("web_search")
    worker_tool = worker.get("web_search")
    assert isinstance(quick_tool, quick.QuickWebSearchTool)
    assert isinstance(worker_tool, WebSearchTool)
    assert isinstance(worker.get("fetch_web_page"), FetchWebPageTool)
    assert isinstance(main.get("fetch_web_page"), FetchWebPageTool)
    assert set(quick_tool.input_schema["properties"]) == {"query"}
    assert set(worker_tool.input_schema["properties"]) == {"query", "limit"}
    assert worker_tool.input_schema["properties"]["limit"]["maximum"] == 10
    assert quick_tool.safety.uses_network is True
    assert quick_tool.safety.risk_level == "low"
    assert quick_tool.safety.requires_confirmation is False
    assert quick_tool.safety.timeout_seconds == 18
    assert replace(quick_tool.safety, timeout_seconds=worker_tool.safety.timeout_seconds,
                   descriptor_hash=worker_tool.safety.descriptor_hash) == worker_tool.safety
    assert quick.SEARCH_REQUEST_TIMEOUT_SECONDS == 15
    assert MAX_WEB_SEARCH_CALLS_PER_TURN == 2


def test_search_keeps_whole_urls_and_first_returned_domain(search, monkeypatch):
    _, execute = search
    url = "https://WWW.Weather.Example.:443/path?q=today%20rain#details"
    rejected = "https://discard.example/" + "q" * 900
    pages = [
        {"url": rejected}, {"url": "https://["},
        {"url": url, "title": "A" * 300, "passage": "B" * 400},
        *[{"url": f"https://others.example/{i}", "title": "晚间天气", "passage": "多云"}
          for i in range(4)],
    ]
    _mock_transport(monkeypatch, lambda request: _response(*pages))
    result = execute(RunContext("thread", "trace"))["result"]
    assert result["status"] == "success"
    assert result["untrusted"] is True and result["trust_notice"]
    assert result["result_count"] == len(result["results"]) == 3
    assert result["primary_domain"] == "weather.example"
    assert result["results"][0] == {"title": "A" * 60, "url": url, "snippet": "B" * 100}
    assert result["results"][-1]["url"] == "https://others.example/1"
    assert len(json.dumps(result, ensure_ascii=False)) <= 900


def test_empty_failure_validation_and_budget(search, monkeypatch):
    _, execute = search
    queries = []

    def provider(request):
        query = json.loads(request.content)["Query"]
        queries.append(query)
        if query == "失败":
            raise httpx.ConnectError("Bearer test-secret: raw provider exception")
        return _response()

    _mock_transport(monkeypatch, provider)
    ctx = RunContext("thread", "trace")
    for invalid in ("   ", "", 5):
        assert execute(ctx, invalid)["error_type"] == "argument_validation_error"
    assert execute(ctx, "query\ud800")["error_type"] == "search_provider_failed"
    monkeypatch.setattr(settings, "aiive_yuanbao_search_api_key", SecretStr(""))
    assert execute(ctx)["error_type"] == "search_provider_failed"
    assert ctx.web_search_calls == len(queries) == 0
    monkeypatch.setattr(settings, "aiive_yuanbao_search_api_key", SecretStr("test-secret"))
    empty = execute(ctx, " 无结果 ")
    assert empty["ok"] is True
    assert empty["result"] == {
        "ok": True, "status": "no_results", "results": [], "result_count": 0,
        "primary_domain": None, "message": "未返回结果",
    }
    failure = execute(ctx, "失败")
    assert failure["ok"] is False and failure["error_type"] == "search_provider_failed"
    assert failure["details"] == {
        "status": "failed", "results": [], "result_count": 0,
        "primary_domain": None, "message": "搜索失败，未返回结果",
    }
    assert "test-secret" not in repr(failure) and "raw provider" not in repr(failure)
    denied = execute(ctx, "第三次")
    assert denied["ok"] is False and denied["error_type"] == "turn_search_budget_exhausted"
    assert denied["details"] == {
        "status": "budget_exhausted", "results": [], "result_count": 0,
        "primary_domain": None, "message": "本轮轻量搜索次数已达上限，本次未执行搜索",
    }
    assert queries == ["无结果", "失败"]
    assert execute(RunContext("thread", "next trace"), "下一轮")["ok"] is True
    assert queries == ["无结果", "失败", "下一轮"]


def test_concurrent_searches_reserve_one_remaining_slot(search, monkeypatch):
    _, execute = search
    hits = []
    _mock_transport(monkeypatch, lambda request: hits.append(request) or _response())
    ctx = RunContext("thread", "trace")
    assert execute(ctx)["ok"]
    barrier = Barrier(2, timeout=5)

    def concurrent_search():
        barrier.wait()
        return execute(ctx)

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(concurrent_search) for _ in range(2)]
        results = [future.result(timeout=5) for future in pending]
    assert sorted(result["ok"] for result in results) == [False, True]
    assert next(result for result in results if not result["ok"])["details"]["status"] == "budget_exhausted"
    assert len(hits) == ctx.web_search_calls == 2


@pytest.mark.parametrize(("request_timeout", "tool_timeout"), [(0.1, 1.0), (5.0, 0.2)])
def test_deadlines_cancel_and_close_stream(search, monkeypatch, request_timeout, tool_timeout):
    registry, execute = search
    tool = registry.get("web_search")
    registry.register(tool.with_definition(replace(
        tool.definition, safety=replace(tool.safety, timeout_seconds=tool_timeout),
    )))
    monkeypatch.setattr(quick, "SEARCH_REQUEST_TIMEOUT_SECONDS", request_timeout)
    started, cancelled, closed = Event(), Event(), Event()

    class DrippingResponse(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            try:
                while True:
                    await asyncio.sleep(0.005)
                    yield b" "
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def aclose(self):
            closed.set()

    _mock_transport(monkeypatch, lambda request: httpx.Response(200, stream=DrippingResponse()))
    ctx = RunContext("thread", "trace")
    for _ in range(2):
        result = execute(ctx)
        assert result["ok"] is False and result["error_type"] == "search_timeout"
        assert result["details"] == {
            "status": "failed", "results": [], "result_count": 0,
            "primary_domain": None, "message": "搜索超时，未返回结果",
        }
        assert started.is_set() and cancelled.is_set() and closed.is_set()
        started.clear()
        cancelled.clear()
        closed.clear()
    assert execute(ctx)["error_type"] == "turn_search_budget_exhausted"
    assert not started.is_set()
    assert ctx.web_search_calls == 2


def test_provider_timeout_closes_real_http_connection(search, monkeypatch):
    _, execute = search
    with socket.socket() as server, ThreadPoolExecutor(max_workers=1) as pool:
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(3)
        monkeypatch.setattr(web_client, "_YUANBAO_SEARCH_URL",
                            f"http://127.0.0.1:{server.getsockname()[1]}/SearchPro")
        monkeypatch.setattr(quick, "SEARCH_REQUEST_TIMEOUT_SECONDS", 0.5)

        def read_until_disconnect():
            connection, _ = server.accept()
            with connection:
                connection.settimeout(3)
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = connection.recv(4096)
                    assert chunk
                    request += chunk
                connection.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\n \r\n")
                while connection.recv(4096):
                    pass

        disconnected = pool.submit(read_until_disconnect)
        assert execute(RunContext("thread", "trace"))["error_type"] == "search_timeout"
        disconnected.result(timeout=3)


@pytest.mark.parametrize("queued", [True, False], ids=["queue", "preflight"])
def test_tool_deadline_expires_before_request_without_spending_budget(search, monkeypatch, queued):
    registry, execute = search
    tool = registry.get("web_search")
    registry.register(tool.with_definition(replace(
        tool.definition, safety=replace(tool.safety, timeout_seconds=0.1),
    )))
    requests = []
    _mock_transport(monkeypatch, lambda request: requests.append(request) or _response())
    release = Event()
    entered_preflight = Event()
    ctx = RunContext("thread", "trace")

    def preflight(_context):
        entered_preflight.set()
        return not release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(executor, "tool_thread_pool", pool)
        monkeypatch.setattr(executor, "cancellation_requested", preflight)
        in_flight = pool.submit(release.wait, 5) if queued else None
        try:
            assert execute(ctx)["error_type"] == "search_timeout"
            if in_flight is not None:
                assert not in_flight.done()
            assert entered_preflight.is_set() is not queued
        finally:
            release.set()
    assert requests == []
    assert ctx.web_search_calls == 0
