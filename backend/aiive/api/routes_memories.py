from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.memory.memory_store import MemoryStore

router = APIRouter(prefix="/api")


class CreateMemoryRequest(BaseModel):
    content: str = Field(..., min_length=1)
    memory_type: str = "fact"
    pinned: bool = False


@router.get("/memories")
def list_memories(db: Session = Depends(get_db)):
    store = MemoryStore(db)
    records = store.list_all()
    return [
        {
            "id": r.id,
            "memory_type": r.memory_type,
            "lifecycle_state": r.lifecycle_state,
            "content": r.content,
            "source_event_id": r.source_event_id,
            "confidence": r.confidence,
            "lineage": r.lineage,
            "pinned": r.pinned,
            "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat(),
        }
        for r in records
    ]


@router.post("/memories")
def create_memory(request: CreateMemoryRequest, db: Session = Depends(get_db)):
    store = MemoryStore(db)
    record = store.create(
        content=request.content,
        memory_type=request.memory_type,
        lifecycle_state="active",
        pinned=request.pinned,
        lineage="manual",
    )
    db.commit()
    return {
        "id": record.id,
        "content": record.content,
        "lifecycle_state": record.lifecycle_state,
    }
