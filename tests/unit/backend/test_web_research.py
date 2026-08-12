"""公开网页研究工具的 SSRF、解析和 Task Scope 测试。"""
from __future__ import annotations

import httpx
import pytest

from aiive.config import settings
from aiive.control.scope import TaskScope
from aiive.control.task_policy import TaskPolicyEngine
from aiive.tools.registry import CapabilitySafetySchema, ToolRegistration
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


def test_duckduckgo_search_parses_and_unwraps_results(monkeypatch) -> None:
    monkeypatch.setattr(web_client, "_resolve_public_host", _public_dns)
    monkeypatch.setattr(settings, "aiive_web_search_provider", "duckduckgo")
    html = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdoc">文档标题</a>'
        '<a class="result__snippet">摘要内容</a>'
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/html"}, text=html, request=request,
    ))
    result = search_web("文档处理", transport=transport)
    assert result["provider"] == "duckduckgo"
    assert result["results"] == [{
        "title": "文档标题", "url": "https://example.com/doc", "snippet": "摘要内容",
    }]
    assert result["untrusted"] is True


def test_network_capability_requires_scope_and_url_host_allowlist() -> None:
    registration = ToolRegistration(
        safety=CapabilitySafetySchema(
            "fetch_web_page", "local_builtin", "trusted", "low", uses_network=True,
        ),
        handler=lambda **_kwargs: None,
    )
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
