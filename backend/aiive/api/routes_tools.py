from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.runtime.event_logger import EventLogger
from aiive.tools.registry import get_tool_registry
from aiive.tools.safe_delete import safe_delete as _safe_delete

router = APIRouter(prefix="/api")


class SafeDeleteRequest(BaseModel):
    path: str = Field(..., min_length=1)
    scope_id: str = Field(..., min_length=1)
    mode: str = "trash"
    trace_id: str | None = None
    thread_id: str | None = None


@router.get("/tools")
def list_tools():
    registry = get_tool_registry()
    return registry.list_all()


@router.post("/tools/safe-delete")
def safe_delete_tool(request: SafeDeleteRequest, db: Session = Depends(get_db)):
    decision = _safe_delete(
        path=request.path,
        scope_id=request.scope_id,
        mode=request.mode,
    )

    # Log as event if trace_id/thread_id provided
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
