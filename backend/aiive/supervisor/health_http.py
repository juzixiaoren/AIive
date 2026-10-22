"""运行中进程的 HTTP 健康探测（stdlib，不走代理）。

静态健康检查（HealthProbe）只验证槽位文件与可导入性；这里验证"进程真的起来了、
真的在响应请求"，是晋升与回滚的最终判据。
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HttpHealth:
    ok: bool
    status: int | None = None
    latency_ms: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "status": self.status, "latency_ms": self.latency_ms, "error": self.error}


def probe_http(
    host: str,
    port: int,
    path: str = "/health",
    *,
    timeout: float = 2.0,
    expected_status: int = 200,
) -> HttpHealth:
    url = f"http://{host}:{port}{path if path.startswith('/') else '/' + path}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.monotonic()
    try:
        with opener.open(url, timeout=max(0.1, timeout)) as response:
            status = int(response.status)
            response.read(1024)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return HttpHealth(
            ok=False,
            status=None,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=f"{exc.__class__.__name__}: {exc}",
        )
    latency = int((time.monotonic() - started) * 1000)
    return HttpHealth(ok=status == expected_status, status=status, latency_ms=latency, error="")


def wait_healthy(
    host: str,
    port: int,
    path: str = "/health",
    *,
    timeout_seconds: float,
    interval: float = 0.2,
    probe_timeout: float = 2.0,
) -> dict[str, Any]:
    """在超时内轮询健康端点，返回最后一次探测结果与尝试次数。"""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    attempts = 0
    last = HttpHealth(ok=False, error="not_probed")
    while True:
        attempts += 1
        last = probe_http(host, port, path, timeout=probe_timeout)
        if last.ok:
            return {**last.as_dict(), "attempts": attempts}
        if time.monotonic() >= deadline:
            return {**last.as_dict(), "attempts": attempts, "timed_out": True}
        time.sleep(interval)
