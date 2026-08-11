"""后端与 Electron Desktop Node 之间的双向 WebSocket 调度器。"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from aiive.config import settings

logger = logging.getLogger(__name__)


class DesktopDispatchError(RuntimeError):
    """桌面节点不在线、超时或返回失败。"""


@dataclass
class _PendingDispatch:
    node_id: str
    action_id: str = ""
    event: threading.Event = field(default_factory=threading.Event)
    acked: bool = False
    idempotent_replay: bool = False
    payload: dict[str, Any] | None = None
    error: str = ""


class DesktopConnectionManager:
    """维护 node_id 到唯一活跃 WebSocket 的映射和同步工具调用等待器。"""

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._pending: dict[str, _PendingDispatch] = {}
        self._lock = threading.Lock()
        self._main_loop: asyncio.AbstractEventLoop | None = None

    def set_main_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._main_loop = loop

    def attach(self, node_id: str, websocket: WebSocket) -> None:
        with self._lock:
            self._connections[node_id] = websocket

    def is_current_connection(self, node_id: str, websocket: WebSocket) -> bool:
        with self._lock:
            return self._connections.get(node_id) is websocket

    def detach(self, node_id: str, websocket: WebSocket) -> bool:
        """仅移除仍为当前代际的连接，避免旧连接关闭把新连接标成离线。"""
        with self._lock:
            if self._connections.get(node_id) is not websocket:
                return False
            self._connections.pop(node_id, None)
            affected = [
                pending for pending in self._pending.values()
                if pending.node_id == node_id and not pending.event.is_set()
            ]
        for pending in affected:
            pending.error = "desktop_node_disconnected"
            pending.event.set()
        return True

    def is_connected(self, node_id: str) -> bool:
        with self._lock:
            return node_id in self._connections

    async def _send(self, node_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            websocket = self._connections.get(node_id)
        if websocket is None:
            raise DesktopDispatchError("desktop_node_offline")
        await websocket.send_text(json.dumps(payload, ensure_ascii=False))

    def dispatch_sync(
        self,
        node_id: str,
        capability_id: str,
        params: dict[str, Any],
        timeout_seconds: float | None = None,
    ) -> Any:
        """从工具工作线程投递到主事件循环，并有限等待节点终态。"""
        if self._main_loop is None or not self.is_connected(node_id):
            raise DesktopDispatchError("desktop_node_offline")
        request_id = str(uuid.uuid4())
        pending = _PendingDispatch(node_id=node_id)
        with self._lock:
            self._pending[request_id] = pending
        timeout = timeout_seconds or settings.aiive_desktop_dispatch_timeout_seconds
        future = asyncio.run_coroutine_threadsafe(
            self._send(node_id, {
                "type": "operation.request",
                "data": {
                    "request_id": request_id,
                    "capability_id": capability_id,
                    "params": params,
                    "timeout_seconds": timeout,
                },
            }),
            self._main_loop,
        )
        try:
            future.result(timeout=min(5.0, timeout))
            if not pending.event.wait(timeout=timeout):
                raise DesktopDispatchError("desktop_operation_timeout")
            if pending.error:
                raise DesktopDispatchError(pending.error)
            return pending.payload
        except DesktopDispatchError:
            raise
        except Exception as error:
            raise DesktopDispatchError(str(error)) from error
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def dispatch_action_sync(
        self,
        *,
        node_id: str,
        task_id: str,
        run_id: str,
        action_id: str,
        idempotency_key: str,
        arguments_hash: str,
        capability_id: str,
        params: dict[str, Any],
        preconditions: dict[str, Any],
        scope: dict[str, Any],
        fencing_token: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """投递 v2 持久 Action；request_id=action_id，重试时 Node journal 幂等回放。"""
        if self._main_loop is None or not self.is_connected(node_id):
            raise DesktopDispatchError("desktop_node_offline")
        request_id = action_id
        pending = _PendingDispatch(node_id=node_id, action_id=action_id)
        with self._lock:
            existing = self._pending.get(request_id)
            if existing is not None and not existing.event.is_set():
                raise DesktopDispatchError("desktop_action_already_in_flight")
            self._pending[request_id] = pending
        timeout = timeout_seconds or settings.aiive_desktop_dispatch_timeout_seconds
        from aiive.desktop.protocol import DESKTOP_PROTOCOL_VERSION, DesktopActionEnvelope

        envelope = DesktopActionEnvelope(
            request_id=request_id,
            action_id=action_id,
            task_id=task_id,
            run_id=run_id,
            idempotency_key=idempotency_key,
            arguments_hash=arguments_hash,
            capability_id=capability_id,
            params=params,
            preconditions=preconditions,
            scope=scope,
            timeout_seconds=timeout,
            fencing_token=fencing_token,
        )
        future = asyncio.run_coroutine_threadsafe(
            self._send(node_id, {
                "type": "operation.request",
                "protocol_version": DESKTOP_PROTOCOL_VERSION,
                "data": envelope.model_dump(mode="json"),
            }),
            self._main_loop,
        )
        try:
            future.result(timeout=min(5.0, timeout))
            if not pending.event.wait(timeout=timeout):
                raise DesktopDispatchError("desktop_operation_timeout")
            if pending.error:
                error_type = (
                    "precondition_failed" if pending.error.startswith("precondition_")
                    else "execution_unknown" if "unknown" in pending.error
                    else "execution_failed"
                )
                return {"ok": False, "error": pending.error, "error_type": error_type}
            return {
                "ok": True,
                "result": pending.payload,
                "action_id": action_id,
                "idempotent_replay": pending.idempotent_replay,
            }
        except DesktopDispatchError:
            raise
        except Exception as error:
            raise DesktopDispatchError(str(error)) from error
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def resolve_ack(self, request_id: str, action_id: str) -> bool:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or (pending.action_id and pending.action_id != action_id):
                return False
            pending.acked = True
            return True

    def resolve_result(
        self,
        request_id: str,
        *,
        node_id: str | None = None,
        action_id: str | None = None,
        ok: bool,
        result: Any = None,
        error: str = "",
        idempotent_replay: bool = False,
    ) -> bool:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return False
            if node_id is not None and pending.node_id != node_id:
                return False
            # v2 持久 Action 必须同时匹配不可变 action_id；不能只凭一个可猜/重放的
            # request_id 就解除另一台 Node 或另一 Action 的等待器。
            if pending.action_id and pending.action_id != action_id:
                return False
            if ok:
                pending.payload = result
                pending.idempotent_replay = idempotent_replay
            else:
                pending.error = error or "desktop_operation_failed"
            pending.event.set()
            return True


desktop_connection_manager = DesktopConnectionManager()
