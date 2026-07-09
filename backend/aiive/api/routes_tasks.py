from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.runtime.task_manager import TaskManager

router = APIRouter(prefix="/api")


class CreateTaskRequest(BaseModel):
    task_type: str = Field(...)
    title: str = Field(..., min_length=1)
    description: str = ""
    condition: str | None = None


@router.post("/tasks")
def create_task(request: CreateTaskRequest, db: Session = Depends(get_db)):
    mgr = TaskManager(db)
    from datetime import datetime, timezone

    task = mgr.create(
        task_type=request.task_type,
        title=request.title,
        description=request.description,
        condition=request.condition,
        next_check_at=datetime.now(timezone.utc),
    )
    db.commit()
    return {"id": task.id, "title": task.title, "status": task.status, "task_type": task.task_type}


@router.get("/tasks")
def list_tasks(status: str | None = Query(None), db: Session = Depends(get_db)):
    mgr = TaskManager(db)
    return [
        {
            "id": t.id,
            "task_type": t.task_type,
            "status": t.status,
            "title": t.title,
            "description": t.description,
            "next_check_at": t.next_check_at.isoformat() if t.next_check_at else None,
            "last_checked_at": t.last_checked_at.isoformat() if t.last_checked_at else None,
        }
        for t in mgr.list_all(status)
    ]


@router.post("/tasks/{task_id}/check-now")
def check_task(task_id: str, db: Session = Depends(get_db)):
    mgr = TaskManager(db)
    result = mgr.check_now(task_id)
    db.commit()
    return result
