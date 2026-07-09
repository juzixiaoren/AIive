"""
API路由模块：聊天接口
- 提供同步和流式聊天的 REST API 接口
- 支持 Server-Sent Events (SSE) 流式响应
"""
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.core.llm_client import LLMClientError
from aiive.db.base import get_db
from aiive.runtime.action_cards import ActionCard, PendingOperation
from aiive.runtime.graph import invoke_chat

router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    """聊天请求体"""
    message: str = Field(..., min_length=1)
    thread_id: str | None = None


class ChatResponse(BaseModel):
    """聊天响应体"""
    reply: str
    thread_id: str
    trace_id: str
    action_cards: list[ActionCard] = []
    pending_operations: list[PendingOperation] = []
    intent_type: str = "plain_chat"
    tool_calls: list[dict] = []
    tool_results: list[dict] = []
    parse_errors: list[str] = []


@router.post("/chat")
def chat(request: ChatRequest, db: Session = Depends(get_db)):
    """同步聊天接口，发送消息并等待完整回复

    Args:
        request: 包含 message 和可选 thread_id 的聊天请求
        db: 数据库会话

    Returns:
        ChatResponse，包含回复、trace_id、action_cards 等
    """
    try:
        result = invoke_chat(message=request.message, thread_id=request.thread_id)
    except LLMClientError:
        db.rollback()
        raise
    return result


@router.post("/chat/stream")
def chat_stream(request: ChatRequest, db: Session = Depends(get_db)):
    """流式聊天接口，使用 Server-Sent Events 实时推送回复

    Args:
        request: 包含 message 和可选 thread_id 的聊天请求
        db: 数据库会话

    Returns:
        StreamingResponse，以 text/event-stream 格式逐条推送事件
    """

    def generate():
        from aiive.core.llm_client import default_llm_client
        from aiive.runtime.agent_loop import AgentLoop

        client = default_llm_client()
        loop = AgentLoop(client, db)
        try:
            for event in loop.run_stream(message=request.message, thread_id=request.thread_id):
                yield f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
        except LLMClientError as e:
            yield f"event: error\ndata: {json.dumps({'message': str(e)}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'message': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
