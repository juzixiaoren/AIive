"""
API路由模块：聊天接口
- 提供同步和流式聊天的 REST API 接口
- 支持 Server-Sent Events (SSE) 流式响应
- Phase 1: ContextAssembler-hard-gate via TurnExecutionService
"""
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from aiive.core.llm_client import LLMClientError, default_llm_client
from aiive.runtime.action_cards import ActionCard, PendingOperation
from aiive.runtime.context_assembler import ContextBudgetExceededError
from aiive.runtime.turn_execution import TurnExecutionService, TurnConflictError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    """聊天请求体"""
    message: str = Field(..., min_length=1)
    thread_id: str | None = None
    turn_id: str | None = None


class SystemChatRequest(BaseModel):
    """系统指令请求"""
    message: str = Field(..., min_length=1)
    thread_id: str


class ChatResponse(BaseModel):
    """聊天响应体"""
    reply: str
    thread_id: str
    trace_id: str
    action_cards: list[ActionCard] = []
    pending_operations: list[PendingOperation] = []
    intent_type: str = "plain_chat"
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    parse_errors: list[str] = []


@router.post("/chat")
def chat(request: ChatRequest):
    """同步聊天接口，通过 TurnExecutionService 执行。"""
    client = default_llm_client()
    service = TurnExecutionService(client, source="user_chat")
    try:
        result = service.execute_turn(
            message=request.message,
            thread_id=request.thread_id,
            turn_id=request.turn_id,
        )
    except LLMClientError:
        raise
    except TurnConflictError as e:
        err_msg = str(e)
        if err_msg == "turn_in_progress":
            raise HTTPException(status_code=202, detail="turn_in_progress")
        raise HTTPException(status_code=409, detail=err_msg)
    if result.get("error") == "context_budget_exceeded":
        raise HTTPException(status_code=413, detail=result)
    if result.get("error"):
        status = result.get("_status", 500)
        raise HTTPException(status_code=status, detail=result.get("error"))
    return result


@router.post("/chat/system")
def chat_system(request: SystemChatRequest):
    """系统指令聊天：通过 TurnExecutionService 统一入口。Phase 1: source='system_command'。"""
    client = default_llm_client()
    import hashlib, json as _json
    operation_id = hashlib.sha256(
        _json.dumps({"source": "system_command", "thread_id": request.thread_id, "message": request.message}, sort_keys=True).encode()
    ).hexdigest()
    turn_id = "system_" + operation_id[:32]

    service = TurnExecutionService(client, source="system_command")
    try:
        result = service.execute_turn(
            message=request.message,
            thread_id=request.thread_id,
            turn_id=turn_id,
        )
    except TurnConflictError as e:
        err_msg = str(e)
        if err_msg == "turn_in_progress":
            raise HTTPException(status_code=202, detail="turn_in_progress")
        raise HTTPException(status_code=409, detail=err_msg)
    if result.get("error"):
        status = result.get("_status", 500)
        raise HTTPException(status_code=status, detail=result.get("error"))
    return result


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """流式聊天接口，通过 TurnExecutionService 执行。"""
    client = default_llm_client()
    service = TurnExecutionService(client, source="user_chat")

    async def generate():
        try:
            result = {"reply": "", "thread_id": "", "trace_id": "", "action_cards": [], "tool_calls": [], "tool_results": []}
            async for event in service.execute_turn_stream(
                message=request.message,
                thread_id=request.thread_id,
                turn_id=request.turn_id,
            ):
                if event.get("type") == "error":
                    if event.get("error") == "context_budget_exceeded":
                        yield f"event: error\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    else:
                        yield f"event: error\ndata: {json.dumps({'message': event.get('error', 'unknown')}, ensure_ascii=False)}\n\n"
                    return
                if event.get("type") == "done":
                    result = event
            if result.get("_turn_cached"):
                yield f"event: done\ndata: {json.dumps({'reply': result.get('reply', ''), 'thread_id': result.get('thread_id', ''), 'trace_id': result.get('trace_id', ''), 'action_cards': result.get('action_cards', []), 'tool_calls': result.get('tool_calls', []), 'tool_results': result.get('tool_results', [])}, ensure_ascii=False)}\n\n"
                return
            yield f"event: done\ndata: {json.dumps({'reply': result.get('reply', ''), 'thread_id': result.get('thread_id', ''), 'trace_id': result.get('trace_id', ''), 'action_cards': result.get('action_cards', []), 'tool_calls': result.get('tool_calls', []), 'tool_results': result.get('tool_results', [])}, ensure_ascii=False)}\n\n"
        except TurnConflictError as e:
            err_msg = str(e)
            yield f"event: error\ndata: {json.dumps({'message': err_msg, '_status': 409 if err_msg != 'turn_in_progress' else 202}, ensure_ascii=False)}\n\n"
        except ContextBudgetExceededError as e:
            yield f"event: error\ndata: {json.dumps({'error': 'context_budget_exceeded', 'safe_tokens': e.safe_tokens, 'context_window': e.context_window}, ensure_ascii=False)}\n\n"
        except LLMClientError as e:
            yield f"event: error\ndata: {json.dumps({'message': str(e)}, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.exception("流式聊天生成异常: thread_id=%s", request.thread_id)
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
