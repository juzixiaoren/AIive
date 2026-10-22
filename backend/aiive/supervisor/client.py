"""应用侧 Supervisor 客户端：提交控制请求、读取状态。

应用进程与 Supervisor 之间只走文件协议，不共享内存、不依赖端口：
- 提交请求后立即返回（切换会重启当前进程，同步等待必然丢失响应）；
- 需要结果时读 results/<request_id>.json，或直接读 state.json / events.jsonl。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from aiive.supervisor.control import (
    ControlPaths,
    find_result_by_key,
    read_events,
    read_json,
    read_result,
    submit_request,
    wait_result,
)
from aiive.supervisor.control import pid_alive


class SupervisorClient:
    def __init__(self, paths: ControlPaths | None = None, *, default_wait: float = 5.0) -> None:
        self.paths: ControlPaths = (paths or ControlPaths.resolve()).ensure()
        self._default_wait: float = default_wait

    def state(self) -> dict[str, Any]:
        return read_json(self.paths.state_file)

    def daemon_pid(self) -> int | None:
        pid = self.state().get("supervisor_pid")
        return pid if isinstance(pid, int) else None

    def available(self) -> bool:
        """守护进程存活即视为可用；仅凭状态文件存在不算。"""
        return pid_alive(self.daemon_pid())

    def status(self, *, events: int = 10) -> dict[str, Any]:
        return {
            "available": self.available(),
            "supervisor_pid": self.daemon_pid(),
            "state": self.state(),
            "events": read_events(self.paths, limit=events),
        }

    def submit(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str = "",
        wait: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if not self.available():
            return {"ok": False, "error": "supervisor_unavailable"}
        if idempotency_key:
            cached = find_result_by_key(self.paths, idempotency_key)
            if cached:
                return {**cached, "deduplicated": True}
        request = submit_request(self.paths, kind, payload, idempotency_key=idempotency_key)
        request_id = str(request["id"])
        if not wait:
            return {
                "ok": True,
                "status": "accepted",
                "request_id": request_id,
                "kind": kind,
            }
        result = wait_result(self.paths, request_id, timeout if timeout is not None else self._default_wait)
        if not result:
            return {
                "ok": True,
                "status": "accepted",
                "request_id": request_id,
                "kind": kind,
                "note": "result_pending",
            }
        return result

    def result(self, request_id: str) -> dict[str, Any]:
        return read_result(self.paths, request_id)


def supervisor_client_from_settings() -> SupervisorClient | None:
    """按配置构造客户端；未启用 Supervisor 时返回 None（走指针切换的兼容路径）。"""
    try:
        from aiive.config import settings
    except Exception:  # pragma: no cover - 配置不可用
        return None
    if not bool(getattr(settings, "supervisor_enabled", False)):
        return None
    return SupervisorClient()


def default_state_dir() -> Path:
    return ControlPaths.resolve().base
