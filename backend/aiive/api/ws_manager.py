"""
WebSocket 连接管理器：管理按 thread_id 分组的 WebSocket 连接。
用于从后端主动推送事件（提醒触发、新消息等）到前端。
"""
import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# 全局通知通道：不绑定具体线程，用于跨线程的未读角标等场景
GLOBAL_THREAD_ID = "__global__"


class ConnectionManager:
    """WebSocket 连接管理器（单例）。

    维护 thread_id → set[WebSocket] 映射，
    支持按线程广播消息和全局广播。
    通过 _main_loop 桥接支持从后台线程安全推送。
    """

    def __init__(self):
        # thread_id → 活跃 WebSocket 连接集合
        self._connections: dict[str, set[WebSocket]] = {}
        self._main_loop: asyncio.AbstractEventLoop | None = None

    def set_main_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定主事件循环，用于从后台线程安全推送。"""
        self._main_loop = loop

    async def connect(self, thread_id: str, ws: WebSocket) -> None:
        """接受 WebSocket 连接并注册到指定线程。"""
        await ws.accept()
        if thread_id not in self._connections:
            self._connections[thread_id] = set()
        self._connections[thread_id].add(ws)

    def disconnect(self, thread_id: str, ws: WebSocket) -> None:
        """移除已断开的连接。"""
        if thread_id in self._connections:
            self._connections[thread_id].discard(ws)
            if not self._connections[thread_id]:
                del self._connections[thread_id]

    async def broadcast_to_thread(self, thread_id: str, event_type: str, data: dict[str, Any]) -> None:
        """向指定线程的所有连接推送事件。"""
        if thread_id not in self._connections:
            return
        payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
        dead: list[WebSocket] = []
        # 迭代副本：send_text 是 await 点，期间并发 connect/disconnect 会修改
        # 活集合，直接迭代原集合会抛 RuntimeError 导致本次广播静默丢失。
        for ws in list(self._connections.get(thread_id, set())):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(thread_id, ws)

    def broadcast_to_thread_sync(self, thread_id: str, event_type: str, data: dict[str, Any]) -> None:
        """线程安全的同步广播入口，供后台 daemon 线程调用。

        通过 asyncio.run_coroutine_threadsafe 将 async 操作投递到主事件循环。
        """
        if self._main_loop is None:
            logger.warning("主事件循环未绑定，跳过广播: thread_id=%s", thread_id)
            return
        asyncio.run_coroutine_threadsafe(
            self.broadcast_to_thread(thread_id, event_type, data),
            self._main_loop,
        )

    async def broadcast_notification(self, pending_count: int) -> None:
        """向全局通知通道推送最新 pending 数量，供前端角标即时更新（替代轮询）。

        Args:
            pending_count: 当前处于 pending 状态的通知数量
        """
        await self.broadcast_to_thread(
            GLOBAL_THREAD_ID, "notification", {"pending_count": pending_count}
        )

    def broadcast_notification_sync(self, pending_count: int) -> None:
        """线程安全的同步推送入口，供后台 daemon 线程调用。"""
        if self._main_loop is None:
            logger.warning("主事件循环未绑定，跳过通知广播")
            return
        asyncio.run_coroutine_threadsafe(
            self.broadcast_notification(pending_count),
            self._main_loop,
        )


# 全局单例
ws_manager = ConnectionManager()
