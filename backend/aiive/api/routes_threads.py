from pydantic import BaseModel

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.runtime.event_logger import EventLogger
from aiive.runtime.thread_state import ThreadState

router = APIRouter(prefix="/api")


class ResetRequest(BaseModel):
    thread_id: str | None = None


@router.post("/thread/reset")
def reset_thread(body: ResetRequest, db: Session = Depends(get_db)):
    """Reset context: record a context_reset event and return success.
    The frontend clears its local state after this call;
    the next /api/chat will create a new thread."""
    logger = EventLogger(db)
    if body.thread_id:
        logger.log_event(
            trace_id=body.thread_id,
            thread_id=body.thread_id,
            event_type="context_reset",
            payload={"reason": "user_requested"},
        )
    db.commit()
    return {"ok": True}


@router.get("/threads/{thread_id}/messages")
def get_thread_messages(thread_id: str, db: Session = Depends(get_db)):
    """Recover messages from backend events for UI display."""
    ts = ThreadState(db)
    msgs = ts.get_recent_messages(thread_id, limit=200)
    return [
        {"role": m["role"], "content": m["content"]}
        for m in msgs
    ]
