"""Phase 6A forget API 路由。"""

from fastapi import APIRouter, HTTPException

from aiive.tools.forget_tool import handle_forget, handle_forget_status

router = APIRouter(prefix="/api/forget", tags=["forget"])


@router.post("/")
def forget_endpoint(
    mode: str = "everywhere",
    memory_ids: list[str] | None = None,
    turn_ids: list[str] | None = None,
    event_ids: list[str] | None = None,
    thread_id: str = "",
    canonical_key: str = "",
    reason: str = "",
):
    """执行 Phase 6A Forget Saga — Phase A 立即屏蔽。"""
    result = handle_forget(
        mode=mode,
        memory_ids=memory_ids,
        turn_ids=turn_ids,
        event_ids=event_ids,
        thread_id=thread_id,
        canonical_key=canonical_key,
        reason=reason,
        requested_by="api",
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "forget failed"))
    return result


@router.get("/{operation_key}/status")
def forget_status_endpoint(
    operation_key: str,
):
    """查询 forget Operation 的各阶段进度。"""
    result = handle_forget_status(operation_key=operation_key)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "not found"))
    return result
