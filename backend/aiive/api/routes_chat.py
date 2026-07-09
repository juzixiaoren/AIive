import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.config import settings
from aiive.core.llm_client import LLMClient, LLMClientError
from aiive.db.base import get_db, SessionLocal
from aiive.runtime.action_cards import ActionCard, PendingOperation
from aiive.runtime.graph import invoke_chat, set_db_factory

router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    thread_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    thread_id: str
    trace_id: str
    action_cards: list[ActionCard] = []
    pending_operations: list[PendingOperation] = []
    intent_type: str = "plain_chat"


@router.post("/chat")
def chat(request: ChatRequest, db: Session = Depends(get_db)):
    set_db_factory(lambda: db)
    try:
        result = invoke_chat(message=request.message, thread_id=request.thread_id)
    except LLMClientError:
        db.rollback()
        raise
    return result


@router.post("/chat/stream")
def chat_stream(request: ChatRequest, db: Session = Depends(get_db)):
    """Streaming chat endpoint using Server-Sent Events."""

    def generate():
        from aiive.runtime.agent_loop import AgentLoop
        from aiive.runtime.thread_state import ThreadState
        from aiive.runtime.trace import Trace

        client = LLMClient(
            base_url=settings.aiive_llm_base_url,
            api_key=settings.aiive_llm_api_key,
            default_model=settings.aiive_llm_model,
            timeout_seconds=settings.aiive_llm_timeout_seconds,
        )
        # Use the same db session
        ts = ThreadState(db)
        thread = ts.get_or_create_thread(request.thread_id)

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
