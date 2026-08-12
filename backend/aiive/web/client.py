"""带 SSRF、重定向、大小和内容类型约束的网页客户端。"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from typing import Any, override
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit

import httpx

from aiive.config import settings


class WebAccessError(ValueError):
    pass


def _validate_public_url(url: str) -> tuple[str, tuple[str, ...]]:
    if len(url) > 4096:
        raise WebAccessError("url_too_long")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as error:
        raise WebAccessError("invalid_url") from error
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise WebAccessError("only_public_http_https_urls_allowed")
    if port not in {None, 80, 443}:
        raise WebAccessError("non_standard_port_blocked")
    normalized_host = host.rstrip(".").casefold()
    if normalized_host == "localhost" or normalized_host.endswith((".local", ".internal", ".localhost")):
        raise WebAccessError("private_host_blocked")
    addresses = _resolve_public_host(
        normalized_host,
        port or (443 if parsed.scheme == "https" else 80),
    )
    return normalized_host, addresses


def _resolve_public_host(host: str, port: int) -> tuple[str, ...]:
    try:
        addresses = {
            str(item[4][0])
            for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as error:
        raise WebAccessError("host_resolution_failed") from error
    if not addresses:
        raise WebAccessError("host_resolution_failed")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise WebAccessError("private_or_reserved_address_blocked")
    return tuple(sorted(addresses))


def _pin_url_to_address(url: str, address: str) -> str:
    """把连接目标固定到已校验 IP，Host/SNI 仍由调用方保留原域名。

    解析和建立连接若分别查询 DNS，会留下 DNS rebinding 的竞态窗口。生产
    请求因此连接到本函数生成的 IP URL；重定向后重新解析并重新固定。
    """
    parsed = urlsplit(url)
    ip = ipaddress.ip_address(address)
    host = f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, ""))


def _read_response(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    transport: httpx.BaseTransport | None = None,
    accepted_types: tuple[str, ...] = ("text/", "application/json", "application/xhtml+xml"),
) -> tuple[str, str, bytes]:
    current = url
    current_params = params
    base_headers = {"User-Agent": settings.aiive_web_user_agent, **(headers or {})}
    for _hop in range(6):
        logical_url = str(httpx.URL(current, params=current_params))
        host, addresses = _validate_public_url(logical_url)
        # Mock/custom transports are already isolated by their caller. Production
        # requests use the exact validated address so DNS cannot change afterward.
        targets = (logical_url,) if transport is not None else tuple(
            _pin_url_to_address(logical_url, address) for address in addresses
        )
        last_transport_error: httpx.TransportError | None = None
        redirected = False
        for target in targets:
            request_headers = dict(base_headers)
            extensions: dict[str, Any] = {}
            if transport is None:
                request_headers["Host"] = urlsplit(logical_url).netloc
                extensions["sni_hostname"] = host
            try:
                with httpx.Client(
                    timeout=settings.aiive_web_request_timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                    transport=transport,
                ) as client:
                    request = client.build_request(
                        "GET",
                        target,
                        headers=request_headers,
                        extensions=extensions,
                    )
                    response = client.send(request, stream=True)
                    try:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise WebAccessError("redirect_without_location")
                            current = urljoin(logical_url, location)
                            current_params = None
                            redirected = True
                            break
                        response.raise_for_status()
                        content_type = response.headers.get(
                            "content-type", "",
                        ).split(";", 1)[0].casefold()
                        if not any(
                            content_type.startswith(prefix) for prefix in accepted_types
                        ):
                            raise WebAccessError(
                                f"unsupported_web_content_type:{content_type or 'missing'}"
                            )
                        chunks: list[bytes] = []
                        size = 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > settings.aiive_web_max_response_bytes:
                                raise WebAccessError("web_response_too_large")
                            chunks.append(chunk)
                        return logical_url, content_type, b"".join(chunks)
                    finally:
                        response.close()
            except httpx.TransportError as error:
                last_transport_error = error
                continue
        if redirected:
            continue
        if last_transport_error is not None:
            raise last_transport_error
        raise WebAccessError("host_resolution_failed")
    raise WebAccessError("too_many_redirects")


def _decode_web(data: bytes, content_type_header: str = "") -> str:
    match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type_header, re.I)
    encodings = [match.group(1)] if match else []
    encodings.extend(["utf-8", "gb18030"])
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


class _PageTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden: int = 0
        self.title_parts: list[str] = []
        self.parts: list[str] = []
        self.in_title: bool = False

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self.hidden += 1
        elif tag == "title":
            self.in_title = True
        elif tag in {"p", "div", "br", "li", "article", "section", "h1", "h2", "h3"}:
            self.parts.append("\n")

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "template"} and self.hidden:
            self.hidden -= 1
        elif tag == "title":
            self.in_title = False

    @override
    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        if self.in_title:
            self.title_parts.append(data)
        self.parts.append(data)

    def result(self) -> tuple[str, str]:
        title = re.sub(r"\s+", " ", " ".join(self.title_parts)).strip()
        text = re.sub(r"[ \t]+", " ", " ".join(self.parts))
        text = re.sub(r" *\n *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return title, text


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._link: dict[str, str] | None = None
        self._snippet: list[str] | None = None

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            self._link = {"title": "", "url": values.get("href") or "", "snippet": ""}
        elif "result__snippet" in classes and self.results:
            self._snippet = []

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link is not None:
            self._link["title"] = re.sub(r"\s+", " ", self._link["title"]).strip()
            if self._link["title"] and self._link["url"]:
                self.results.append(self._link)
            self._link = None
        if self._snippet is not None and tag in {"a", "div", "span"}:
            if self.results:
                self.results[-1]["snippet"] = re.sub(r"\s+", " ", " ".join(self._snippet)).strip()
            self._snippet = None

    @override
    def handle_data(self, data: str) -> None:
        if self._link is not None:
            self._link["title"] += data
        elif self._snippet is not None:
            self._snippet.append(data)


def _unwrap_ddg_url(url: str) -> str:
    absolute = urljoin("https://duckduckgo.com", url)
    uddg = parse_qs(urlsplit(absolute).query).get("uddg")
    return unquote(uddg[0]) if uddg else absolute


def fetch_web_page(
    url: str,
    *,
    max_chars: int = 100_000,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    final_url, content_type, data = _read_response(url, transport=transport)
    decoded = _decode_web(data)
    if content_type == "application/json":
        try:
            text = json.dumps(json.loads(decoded), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            text = decoded
        title = ""
    elif "html" in content_type or content_type == "application/xhtml+xml":
        parser = _PageTextParser()
        parser.feed(decoded)
        title, text = parser.result()
    else:
        title, text = "", decoded.strip()
    limit = max(1, min(max_chars, 500_000))
    return {
        "ok": True, "url": final_url, "title": title, "content_type": content_type,
        "text": text[:limit], "truncated": len(text) > limit, "untrusted": True,
    }


def search_web(
    query: str,
    *,
    limit: int = 5,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    if not settings.aiive_web_search_enabled:
        raise WebAccessError("web_search_disabled")
    query = query.strip()
    if not query:
        raise WebAccessError("empty_search_query")
    limit = max(1, min(limit, 10))
    provider = settings.aiive_web_search_provider
    if provider == "brave":
        _url, _content_type, data = _read_response(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"Accept": "application/json", "X-Subscription-Token": settings.aiive_brave_search_api_key},
            params={"q": query, "count": limit}, transport=transport,
        )
        payload = json.loads(_decode_web(data))
        raw_results = (payload.get("web") or {}).get("results") or []
        results = [
            {"title": str(item.get("title", "")), "url": str(item.get("url", "")),
             "snippet": str(item.get("description", ""))}
            for item in raw_results[:limit] if isinstance(item, dict)
        ]
    elif provider == "searxng":
        base = settings.aiive_searxng_base_url.rstrip("/") + "/search"
        _url, _content_type, data = _read_response(
            base, params={"q": query, "format": "json"}, transport=transport,
        )
        payload = json.loads(_decode_web(data))
        results = [
            {"title": str(item.get("title", "")), "url": str(item.get("url", "")),
             "snippet": str(item.get("content", ""))}
            for item in (payload.get("results") or [])[:limit] if isinstance(item, dict)
        ]
    else:
        _url, _content_type, data = _read_response(
            "https://html.duckduckgo.com/html/", params={"q": query}, transport=transport,
            accepted_types=("text/html",),
        )
        parser = _DuckDuckGoParser()
        parser.feed(_decode_web(data))
        results = [
            {**item, "url": _unwrap_ddg_url(item["url"])} for item in parser.results[:limit]
        ]
    return {
        "ok": True, "query": query, "provider": provider, "results": results,
        "total": len(results), "untrusted": True,
    }
