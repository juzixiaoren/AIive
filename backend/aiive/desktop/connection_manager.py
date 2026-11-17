"""后端与 Electron Desktop Node 之间的双向 WebSocket 调度器。"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from aiive.config import settings
from aiive.desktop.protocol import DesktopDispatchPhase

logger = logging.getLogger(__name__)


class DesktopDispatchError(RuntimeError):
    """桌面节点不在线、超时、断连或返回失败。

    phase 标明这次派发相对"请求已写往 Node"边界的位置，调用方据此区分
    确定性未执行与外部副作用未知；默认 SUBMITTED（无法确认即按未知处理）。
    """

    def __init__(
        self, message: str, phase: DesktopDispatchPhase = DesktopDispatchPhase.SUBMITTED,
    ) -> None:
        super().__init__(message)
        self.phase: DesktopDispatchPhase = phase


@dataclass
class _PendingDispatch:
    node_id: str
    action_id: str = ""
    event: threading.Event = field(default_factory=threading.Event)
    acked: bool = False
    idempotent_replay: bool = False
    payload: dict[str, Any] | None = None
    error: str = ""


def classify_pending_error(error: str) -> str:
    """把 Node 回报的失败原因映射为工具执行错误码。

    取消与前置条件失败是确定性结论；断连以及任何标注 "unknown" 的原因都
    代表外部副作用真实结果未知，不能降级成普通失败或成功。
    """
    if error == "cancelled_before_start":
        return "cancelled"
    if error.startswith("precondition_"):
        return "precondition_failed"
    if error == "desktop_node_disconnected" or "unknown" in error:
        return "execution_unknown"
    return "execution_failed"


class DesktopConnectionManager:
    """维护 node_id 到唯一活跃 WebSocket 的映射和同步工具调用等待器。"""

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._pending: dict[str, _PendingDispatch] = {}
        self._lock: threading.Lock = threading.Lock()
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
            # 没有 socket：请求一定没有写出去。
            raise DesktopDispatchError(
                "desktop_node_offline", phase=DesktopDispatchPhase.NOT_SUBMITTED,
            )
        try:
            await websocket.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception as error:
            # 写 socket 过程中失败：对端可能已经收到请求，只能判为未知。
            raise DesktopDispatchError(
                f"desktop_send_failed: {error}", phase=DesktopDispatchPhase.SUBMITTED,
            ) from error

    def _discard_pending(self, request_id: str, pending: _PendingDispatch) -> None:
        """按对象身份移除本次创建的 pending。

        同 request_id 可能已经有新的 pending（重试或新的派发），旧请求的收尾
        不能误删它；也不改变本次已经判定的执行阶段。
        """
        with self._lock:
            if self._pending.get(request_id) is pending:
                self._pending.pop(request_id, None)

    def _submit_to_loop(self, coroutine: Any) -> Any:
        """投递协程到主事件循环；循环不可用时请求尚未发出。"""
        loop = self._main_loop
        if loop is not None:
            try:
                return asyncio.run_coroutine_threadsafe(coroutine, loop)
            except RuntimeError as error:
                message = f"main_loop_unavailable: {error}"
        else:
            message = "main_loop_unavailable"
        close = getattr(coroutine, "close", None)
        if callable(close):
            close()
        raise DesktopDispatchError(message, phase=DesktopDispatchPhase.NOT_SUBMITTED)

    def dispatch_sync(
        self,
        node_id: str,
        capability_id: str,
        params: dict[str, Any],
        timeout_seconds: float | None = None,
    ) -> Any:
        """从工具工作线程投递到主事件循环，并有限等待节点终态。"""
        if self._main_loop is None:
            raise DesktopDispatchError(
                "main_loop_unavailable", phase=DesktopDispatchPhase.NOT_SUBMITTED,
            )
        if not self.is_connected(node_id):
            raise DesktopDispatchError(
                "desktop_node_offline", phase=DesktopDispatchPhase.NOT_SUBMITTED,
            )
        request_id = str(uuid.uuid4())
        pending = _PendingDispatch(node_id=node_id)
        timeout = timeout_seconds or settings.aiive_desktop_dispatch_timeout_seconds
        with self._lock:
            self._pending[request_id] = pending
        try:
            # 协程提交也在保护范围内：循环不可用时请求没有发出，必须清掉 pending。
            future = self._submit_to_loop(
                self._send(node_id, {
                    "type": "operation.request",
                    "data": {
                        "request_id": request_id,
                        "capability_id": capability_id,
                        "params": params,
                        "timeout_seconds": timeout,
                    },
                })
            )
            future.result(timeout=min(5.0, timeout))
            if not pending.event.wait(timeout=timeout):
                raise DesktopDispatchError(
                    "desktop_operation_timeout", phase=DesktopDispatchPhase.SUBMITTED,
                )
            if pending.error:
                # Node 已经返回明确终态：按回执处理。
                raise DesktopDispatchError(
                    pending.error, phase=DesktopDispatchPhase.TERMINAL,
                )
            return pending.payload
        except DesktopDispatchError:
            raise
        except Exception as error:
            # 发送/等待期间的未知异常：对端可能已经收到请求。
            raise DesktopDispatchError(
                str(error), phase=DesktopDispatchPhase.SUBMITTED,
            ) from error
        finally:
            self._discard_pending(request_id, pending)

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
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """投递 v2 持久 Action；request_id=action_id，重试时 Node journal 幂等回放。"""
        if self._main_loop is None:
            raise DesktopDispatchError(
                "main_loop_unavailable", phase=DesktopDispatchPhase.NOT_SUBMITTED,
            )
        if not self.is_connected(node_id):
            raise DesktopDispatchError(
                "desktop_node_offline", phase=DesktopDispatchPhase.NOT_SUBMITTED,
            )
        timeout = timeout_seconds or settings.aiive_desktop_dispatch_timeout_seconds
        from aiive.desktop.protocol import DESKTOP_PROTOCOL_VERSION, DesktopActionEnvelope

        request_id = action_id
        try:
            # 发送前完成信封校验：本地参数/身份不合法时请求一定不会发出。
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
        except Exception as error:
            raise DesktopDispatchError(
                f"desktop_envelope_invalid: {error}",
                phase=DesktopDispatchPhase.NOT_SUBMITTED,
            ) from error

        pending = _PendingDispatch(node_id=node_id, action_id=action_id)
        with self._lock:
            existing = self._pending.get(request_id)
            if existing is not None and not existing.event.is_set():
                # 同一个 Action 已有在途请求：本次没有发送，但真实结果未知。
                raise DesktopDispatchError(
                    "desktop_action_already_in_flight",
                    phase=DesktopDispatchPhase.SUBMITTED,
                )
            self._pending[request_id] = pending
        try:
            # 协程提交也在保护范围内：循环不可用时请求没有发出，必须清掉 pending。
            future = self._submit_to_loop(
                self._send(node_id, {
                    "type": "operation.request",
                    "protocol_version": DESKTOP_PROTOCOL_VERSION,
                    "data": envelope.model_dump(mode="json"),
                })
            )
            future.result(timeout=min(5.0, timeout))
            deadline = time.monotonic() + timeout
            cancel_sent = False
            while not pending.event.wait(timeout=min(0.1, max(0, deadline-time.monotonic()))):
                if time.monotonic() >= deadline:
                    raise DesktopDispatchError(
                        "desktop_operation_timeout", phase=DesktopDispatchPhase.SUBMITTED,
                    )
                if not cancel_sent and cancel_check is not None and cancel_check():
                    # 取消投递失败不能改变本次派发的结论：请求已经在 Node 侧，
                    # 继续等待真实终态，最终由 Journal 对账兜底。
                    try:
                        self.cancel_action_sync(node_id, action_id, request_id, fencing_token)
                    except Exception:
                        logger.warning(
                            "Desktop 取消投递失败: action_id=%s", action_id, exc_info=True,
                        )
                    cancel_sent = True
            if pending.error:
                return {
                    "ok": False,
                    "error": pending.error,
                    "error_type": classify_pending_error(str(pending.error)),
                }
            return {
                "ok": True,
                "result": pending.payload,
                "action_id": action_id,
                "idempotent_replay": pending.idempotent_replay,
            }
        except DesktopDispatchError:
            raise
        except Exception as error:
            # 发送/等待期间的未知异常：对端可能已经收到请求。
            raise DesktopDispatchError(
                str(error), phase=DesktopDispatchPhase.SUBMITTED,
            ) from error
        finally:
            self._discard_pending(request_id, pending)

    def cancel_action_sync(self, node_id: str, action_id: str, request_id: str, fencing_token: str) -> None:
        from aiive.desktop.protocol import DesktopCancelEnvelope, DESKTOP_PROTOCOL_VERSION
        if self._main_loop is None:
            raise DesktopDispatchError(
                "main_loop_unavailable", phase=DesktopDispatchPhase.NOT_SUBMITTED,
            )
        envelope = DesktopCancelEnvelope(action_id=action_id, request_id=request_id, fencing_token=fencing_token)
        future = asyncio.run_coroutine_threadsafe(self._send(node_id, {
            "type": "operation.cancel", "protocol_version": DESKTOP_PROTOCOL_VERSION,
            "data": envelope.model_dump(),
        }), self._main_loop)
        future.result(timeout=5)

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
