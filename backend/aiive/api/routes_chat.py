"""
API路由模块：聊天接口
- 提供同步和流式聊天的 REST API 接口
- 支持 Server-Sent Events (SSE) 流式响应
- Phase 1: ContextAssembler-hard-gate via TurnExecutionService
"""
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from aiive.core.llm_client import LLMClientError, default_llm_client
from aiive.runtime.action_cards import ChatResponse
from aiive.runtime.context_assembler import ContextBudgetExceededError
from aiive.runtime.turn_execution import TurnExecutionService, TurnConflictError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _llm_error_payload(error: LLMClientError) -> dict[str, object]:
    """构建同步与 SSE 共用的安全 LLM 错误结构。"""
    return {
        "code": error.code,
        "message": str(error),
        "retryable": error.retryable,
        "trace_id": error.trace_id,
        "retry_after_seconds": error.retry_after_seconds,
    }


def _raise_llm_http_error(error: LLMClientError) -> None:
    """将 LLM 错误转换为稳定的 HTTP 状态和响应体。"""
    raise HTTPException(
        status_code=error.status_code or 503,
        detail=_llm_error_payload(error),
    )


class ChatRequest(BaseModel):
    """聊天请求体"""
    message: str = Field(..., min_length=1)
    thread_id: str | None = None
    turn_id: str | None = None


class SystemChatRequest(BaseModel):
    """系统指令请求"""
    message: str = Field(..., min_length=1)
    thread_id: str




@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    """同步聊天接口，通过 TurnExecutionService 执行。"""
    client = default_llm_client()
    service = TurnExecutionService(client, source="user_chat")
    try:
        result = service.execute_turn(
            message=request.message,
            thread_id=request.thread_id,
            turn_id=request.turn_id,
        )
    except LLMClientError as error:
        _raise_llm_http_error(error)
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
    return ChatResponse.model_validate(result)


@router.post("/chat/system", response_model=ChatResponse)
def chat_system(request: SystemChatRequest) -> ChatResponse:
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
    except LLMClientError as error:
        _raise_llm_http_error(error)
    except TurnConflictError as e:
        err_msg = str(e)
        if err_msg == "turn_in_progress":
            raise HTTPException(status_code=202, detail="turn_in_progress")
        raise HTTPException(status_code=409, detail=err_msg)
    if result.get("error"):
        status = result.get("_status", 500)
        raise HTTPException(status_code=status, detail=result.get("error"))
    return ChatResponse.model_validate(result)


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
                    error_payload = {
                        "code": event.get("error", "internal_error"),
                        "message": event.get("message", event.get("error", "请求失败")),
                        "retryable": event.get("retryable", False),
                        "trace_id": event.get("trace_id"),
                        "retry_after_seconds": event.get("retry_after_seconds"),
                        "status": event.get("_status", 500),
                    }
                    if event.get("error") == "context_budget_exceeded":
                        error_payload.update({
                            "safe_tokens": event.get("safe_tokens"),
                            "context_window": event.get("context_window"),
                        })
                    yield f"event: error\ndata: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
                    return
                if event.get("type") == "token":
                    yield f"event: token\ndata: {json.dumps({'text': event.get('text', '')}, ensure_ascii=False)}\n\n"
                elif event.get("type") == "tool_call":
                    yield f"event: tool_call\ndata: {json.dumps({'tool_call_id': event.get('tool_call_id', ''), 'name': event.get('name', ''), 'params': event.get('params', {})}, ensure_ascii=False)}\n\n"
                elif event.get("type") == "tool_result":
                    yield f"event: tool_result\ndata: {json.dumps({'tool_call_id': event.get('tool_call_id', ''), 'name': event.get('name', ''), 'result': event.get('result', ''), 'status': event.get('status', 'completed')}, ensure_ascii=False)}\n\n"
                elif event.get("type") == "done":
                    result = event
            response = ChatResponse.model_validate({
                "reply": result.get("reply", ""),
                "thread_id": result.get("thread_id", ""),
                "trace_id": result.get("trace_id", ""),
                "action_cards": result.get("action_cards", []),
                "pending_operations": result.get("pending_operations", []),
                "tool_calls": result.get("tool_calls", []),
                "tool_results": result.get("tool_results", []),
                "parse_errors": result.get("parse_errors", []),
            })
            yield f"event: done\ndata: {response.model_dump_json(exclude_none=True)}\n\n"
        except TurnConflictError as error:
            message = str(error)
            payload = {"code": message, "message": message, "retryable": True,
                       "trace_id": None, "retry_after_seconds": None,
                       "status": 409 if message != "turn_in_progress" else 202}
            yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except ContextBudgetExceededError as error:
            payload = {"code": "context_budget_exceeded", "message": "上下文超过模型安全预算",
                       "retryable": False, "trace_id": None, "retry_after_seconds": None,
                       "status": 413, "safe_tokens": error.safe_tokens,
                       "context_window": error.context_window}
            yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except LLMClientError as error:
            payload = _llm_error_payload(error)
            payload["status"] = error.status_code or 503
            yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except Exception:
            logger.exception("流式聊天生成异常: thread_id=%s", request.thread_id)
            payload = {"code": "internal_error", "message": "聊天请求处理失败",
                       "retryable": True, "trace_id": None,
                       "retry_after_seconds": None, "status": 500}
            yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
