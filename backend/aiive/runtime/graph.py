"""
运行时层 - LangGraph 适配器。

为 AIive 聊天管线提供 invoke_chat() 入口，内部委托给 TurnExecutionService。
"""
from typing import Any


def invoke_chat(message: str, thread_id: str | None = None) -> dict[str, Any]:
    """通过 TurnExecutionService 执行单次聊天轮次。

    由 POST /api/chat 调用。
    """
    from aiive.core.llm_client import default_llm_client
    from aiive.runtime.turn_execution import TurnExecutionService

    client = default_llm_client()
    service = TurnExecutionService(client)
    result = service.execute_turn(message=message, thread_id=thread_id)
    # 过滤内部状态字段
    if result.get("_status"):
        status_code = result.pop("_status")
        if status_code == 409:
            from fastapi import HTTPException
            raise HTTPException(status_code=409, detail=result.get("error", "conflict"))
        if status_code == 202:
            from fastapi import HTTPException
            raise HTTPException(status_code=202, detail=result.get("error", "turn_in_progress"))
    return result
