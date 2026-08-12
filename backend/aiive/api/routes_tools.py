"""
API路由模块：工具管理
- 提供工具注册表查询接口
- 提供安全删除（safe-delete）接口
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.db.models import ToolOperation
from aiive.tools.registry import get_tool_registry

router = APIRouter(prefix="/api")


class SafeDeleteRequest(BaseModel):
    """安全删除请求体"""
    path: str = Field(..., min_length=1)
    scope_id: str = Field(..., min_length=1)
    mode: str = "trash"
    trace_id: str | None = None
    thread_id: str | None = None


@router.get("/tools")
def list_tools():
    """获取所有已注册工具列表

    Returns:
        工具列表，包含每个工具的名称、描述、参数等信息
    """
    registry = get_tool_registry()
    return registry.list_all()


@router.get("/tool-operations/{operation_id}")
def get_tool_operation(
    operation_id: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """查询副作用工具操作的真实当前状态和已确认结果。"""
    operation = db.get(ToolOperation, operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="工具操作不存在")
    return {
        "operation_id": operation.id,
        "capability_id": operation.capability_id,
        "status": operation.status,
        "trace_id": operation.trace_id,
        "tool_call_id": operation.tool_call_id,
        "result": operation.result_payload if operation.status == "committed" else None,
        "error": operation.error_message,
        "terminal_reason": operation.terminal_reason,
        "created_at": operation.created_at,
        "started_at": operation.started_at,
        "completed_at": operation.completed_at,
    }


@router.post("/tools/safe-delete")
def safe_delete_tool(_request: SafeDeleteRequest) -> dict[str, Any]:
    """拒绝绕过 ToolRegistry、审批和持久化 operation 的直接删除入口。"""
    raise HTTPException(
        status_code=409,
        detail="安全删除必须通过 Chat 的 safe_delete 工具和审批流程执行",
    )
