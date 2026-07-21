"""
WebSocket 路由：前端通过 WS 连接接收后端主动推送的事件。
"""
import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from aiive.api.routes_notifications import count_pending_notifications
from aiive.api.ws_manager import GLOBAL_THREAD_ID, ws_manager
from aiive.db.base import SessionLocal

router = APIRouter()


@router.websocket("/ws/{thread_id}")
async def websocket_endpoint(ws: WebSocket, thread_id: str):
    """WebSocket 端点：前端按 thread_id 连接，接收新消息和事件推送。

    全局通知通道（thread_id == GLOBAL_THREAD_ID）连接建立时，立即推送一次当前
    pending 数量快照，避免前端角标出现空窗（无需额外 HTTP 请求）。

    Args:
        ws: WebSocket 连接对象
        thread_id: 会话线程 ID
    """
    await ws_manager.connect(thread_id, ws)
    try:
        if thread_id == GLOBAL_THREAD_ID:
            # 连接即推送当前 pending 数量，避免角标空窗（用短生命周期会话，不占用连接）
            snap_db = SessionLocal()
            try:
                pending_count = count_pending_notifications(snap_db)
            finally:
                snap_db.close()
            snapshot = json.dumps(
                {
                    "type": "notification",
                    "data": {"pending_count": pending_count},
                },
                ensure_ascii=False,
            )
            await ws.send_text(snapshot)
        # 保持连接，接收客户端消息（心跳等）
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(thread_id, ws)
