from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.config import settings
from aiive.core.llm_client import LLMClient, LLMClientError
from aiive.db.base import get_db
from aiive.runtime.agent_loop import AgentLoop

router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    thread_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    thread_id: str
    trace_id: str


def _build_client() -> LLMClient:
    return LLMClient(
        base_url=settings.aiive_llm_base_url,
        api_key=settings.aiive_llm_api_key,
        default_model=settings.aiive_llm_model,
        timeout_seconds=settings.aiive_llm_timeout_seconds,
    )


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, db: Session = Depends(get_db)):
    client = _build_client()
    loop = AgentLoop(client, db)
    try:
        result = loop.run(message=request.message, thread_id=request.thread_id)
    except LLMClientError:
        db.rollback()
        raise
    return ChatResponse(**result)
