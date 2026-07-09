"""
WebSocket 路由：前端通过 WS 连接接收后端主动推送的事件。
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from aiive.api.ws_manager import ws_manager

router = APIRouter()


@router.websocket("/ws/{thread_id}")
async def websocket_endpoint(ws: WebSocket, thread_id: str):
    """WebSocket 端点：前端按 thread_id 连接，接收新消息和事件推送。

    Args:
        ws: WebSocket 连接对象
        thread_id: 会话线程 ID
    """
    await ws_manager.connect(thread_id, ws)
    try:
        # 保持连接，接收客户端消息（心跳等）
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(thread_id, ws)
