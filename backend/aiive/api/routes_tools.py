"""
API路由模块：工具管理
- 提供工具注册表查询接口
- 提供安全删除（safe-delete）接口
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.runtime.event_logger import EventLogger
from aiive.tools.registry import get_tool_registry
from aiive.tools.safe_delete import safe_delete as _safe_delete

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


@router.post("/tools/safe-delete")
def safe_delete_tool(request: SafeDeleteRequest, db: Session = Depends(get_db)):
    """安全删除指定路径的文件或目录

    执行删除前会进行安全校验，返回是否允许删除及原因。
    如果提供了 trace_id 和 thread_id，会记录删除事件。

    Args:
        request: 包含 path、scope_id、mode 的删除请求
        db: 数据库会话

    Returns:
        删除决策，包含 allowed、reason、resolved_path 等
    """
    decision = _safe_delete(
        path=request.path,
        scope_id=request.scope_id,
        mode=request.mode,
    )

    # 如果提供了 trace_id 和 thread_id，记录事件
    if request.trace_id and request.thread_id:
        logger = EventLogger(db)
        logger.log_event(
            trace_id=request.trace_id,
            thread_id=request.thread_id,
            event_type="delete_request",
            payload={
                "path": request.path,
                "scope_id": request.scope_id,
                "mode": request.mode,
                "allowed": decision.allowed,
                "reason": decision.reason,
                "resolved_path": decision.resolved_path,
            },
        )
        db.commit()

    return {
        "allowed": decision.allowed,
        "reason": decision.reason,
        "resolved_path": decision.resolved_path,
        "scope_id": decision.scope_id,
        "mode": decision.mode,
    }
