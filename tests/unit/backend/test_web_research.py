"""公开网页研究工具的 SSRF、解析和 Task Scope 测试。"""
from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from aiive.config import settings
from aiive.control.scope import TaskScope
from aiive.control.task_policy import TaskPolicyEngine
from aiive.tools.base import BaseTool
from aiive.tools.registry import CapabilitySafetySchema, ToolDefinition
from aiive.web import client as web_client
from aiive.web.client import WebAccessError, fetch_web_page, search_web


def _public_dns(_host: str, _port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


def test_web_fetch_blocks_private_targets() -> None:
    with pytest.raises(WebAccessError, match="private_host_blocked"):
        fetch_web_page("http://localhost/admin")
    with pytest.raises(WebAccessError, match="private_or_reserved_address_blocked"):
        fetch_web_page("http://127.0.0.1/admin")


def test_validated_hostname_is_pinned_to_the_resolved_address() -> None:
    assert web_client._pin_url_to_address(
        "https://example.com:443/a?q=1#ignored",
        "93.184.216.34",
    ) == "https://93.184.216.34:443/a?q=1"


def test_web_fetch_extracts_visible_text(monkeypatch) -> None:
    monkeypatch.setattr(web_client, "_resolve_public_host", _public_dns)
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        text="<title>Example</title><script>ignore()</script><article>可信事实</article>",
        request=request,
    ))
    result = fetch_web_page("https://example.com/article", transport=transport)
    assert result["title"] == "Example"
    assert "可信事实" in result["text"]
    assert "ignore" not in result["text"]
    assert result["untrusted"] is True


@pytest.fixture
def yuanbao_settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "aiive_web_search_enabled", True)
    monkeypatch.setattr(settings, "aiive_yuanbao_search_api_key", SecretStr("test-key"))


def test_yuanbao_search_authenticates_and_normalizes_results(yuanbao_settings) -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://api.wsa.cloud.tencent.com/SearchPro"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {"Query": "文档处理"}
        return httpx.Response(200, json={"Response": {"Pages": [
            "not-json",
            json.dumps({"url": "javascript:void(0)"}),
            json.dumps({"title": "文档标题", "url": "https://example.com/doc", "passage": "摘要内容"}),
            json.dumps({"title": "第二条", "url": "https://example.com/other"}),
        ]}})

    result = search_web(" 文档处理 ", limit=1, transport=httpx.MockTransport(handle_request))
    assert result["provider"] == "yuanbao"
    assert result["query"] == "文档处理"
    assert result["total"] == 1
    assert result["results"] == [{
        "title": "文档标题", "url": "https://example.com/doc", "snippet": "摘要内容",
    }]
    assert result["untrusted"] is True


def test_yuanbao_search_missing_key_does_not_make_request(monkeypatch, yuanbao_settings) -> None:
    monkeypatch.setattr(settings, "aiive_yuanbao_search_api_key", SecretStr(""))
    transport = httpx.MockTransport(lambda request: pytest.fail("unexpected HTTP request"))
    with pytest.raises(WebAccessError, match="AIIVE_YUANBAO_SEARCH_API_KEY"):
        search_web("query", transport=transport)


def test_yuanbao_search_does_not_forward_key_on_redirect(yuanbao_settings) -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.wsa.cloud.tencent.com"
        return httpx.Response(307, headers={"Location": "https://example.com/"})

    with pytest.raises(WebAccessError, match="yuanbao_search_http_error:307"):
        search_web("query", transport=httpx.MockTransport(handle_request))


def test_yuanbao_search_reports_api_error_without_echoing_message(yuanbao_settings) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "Response": {"Error": {"Code": "AuthFailure", "Message": "Bearer test-key"}},
    }))
    with pytest.raises(WebAccessError, match="^yuanbao_search_api_error:AuthFailure$"):
        search_web("query", transport=transport)


def test_yuanbao_search_caps_response_size(monkeypatch, yuanbao_settings) -> None:
    monkeypatch.setattr(settings, "aiive_web_max_response_bytes", 1024)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b" " * 1025))
    with pytest.raises(WebAccessError, match="web_response_too_large"):
        search_web("query", transport=transport)


def test_network_capability_requires_scope_and_url_host_allowlist() -> None:
    class FakeTool23(BaseTool):

        def _run(self, **_kwargs):
            return None
    registration = FakeTool23(ToolDefinition(safety=CapabilitySafetySchema('fetch_web_page', 'local_builtin', 'trusted', 'low', uses_network=True)))
    denied_scope = TaskScope(
        allowed_capabilities=frozenset({"fetch_web_page"}), allowed_roots=(),
        allowed_hosts=frozenset(), allow_network=False,
    )
    assert TaskPolicyEngine().decide(
        registration, denied_scope, "fetch_web_page", {"url": "https://example.com"},
    ).reason == "network_access_not_in_task_scope"

    host_scope = TaskScope(
        allowed_capabilities=frozenset({"fetch_web_page"}), allowed_roots=(),
        allowed_hosts=frozenset({"example.com"}), allow_network=True,
    )
    assert host_scope.validate_arguments(
        "fetch_web_page", {"url": "https://docs.example.com/page"},
    ) == []
    assert "host_outside_task_scope:url" in host_scope.validate_arguments(
        "fetch_web_page", {"url": "https://evil.example.net/page"},
    )
