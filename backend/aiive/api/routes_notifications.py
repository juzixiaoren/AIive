from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.db.models import Event

router = APIRouter(prefix="/api")


@router.get("/notifications")
def list_notifications(
    status: str | None = Query(None),
    db: Session = Depends(get_db),
):
    q = (
        db.query(Event)
        .filter(Event.event_type == "notification_created")
        .order_by(Event.created_at.desc())
        .limit(50)
    )
    events = q.all()
    return [
        {
            "id": e.id,
            "task_id": e.payload.get("task_id"),
            "title": e.payload.get("title", ""),
            "message": e.payload.get("message", ""),
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]
