from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.db.models import MemoryRecord
from aiive.memory.memory_types import PERSONAL_SIGNAL_TYPES

router = APIRouter(prefix="/api")


@router.get("/personal-signals")
def list_personal_signals(db: Session = Depends(get_db)):
    records = (
        db.query(MemoryRecord)
        .filter(MemoryRecord.memory_type.in_(PERSONAL_SIGNAL_TYPES))
        .order_by(MemoryRecord.updated_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id": r.id,
            "memory_type": r.memory_type,
            "content": r.content,
            "lifecycle_state": r.lifecycle_state,
            "confidence": r.confidence,
            "lineage": r.lineage,
            "created_at": r.created_at.isoformat(),
        }
        for r in records
    ]
