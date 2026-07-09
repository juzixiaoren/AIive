"""
WebSocket 连接管理器：管理按 thread_id 分组的 WebSocket 连接。
用于从后端主动推送事件（提醒触发、新消息等）到前端。
"""
import json
from fastapi import WebSocket


class ConnectionManager:
    """WebSocket 连接管理器（单例）。

    维护 thread_id → set[WebSocket] 映射，
    支持按线程广播消息和全局广播。
    """

    def __init__(self):
        # thread_id → 活跃 WebSocket 连接集合
        self._connections: dict[str, set[WebSocket]] = {}

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

    async def broadcast_to_thread(self, thread_id: str, event_type: str, data: dict) -> None:
        """向指定线程的所有连接推送事件。"""
        if thread_id not in self._connections:
            return
        payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
        dead: list[WebSocket] = []
        for ws in self._connections.get(thread_id, set()):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(thread_id, ws)

    async def broadcast_all(self, event_type: str, data: dict) -> None:
        """向所有连接推送事件。"""
        for thread_id in list(self._connections.keys()):
            await self.broadcast_to_thread(thread_id, event_type, data)


# 全局单例
ws_manager = ConnectionManager()
