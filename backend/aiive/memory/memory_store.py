from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy.orm import Session

from aiive.db.models import MemoryRecord


class MemoryStore:
    def __init__(self, db: Session):
        self._db = db

    def create(
        self,
        content: str,
        memory_type: str = "fact",
        lifecycle_state: str = "candidate",
        source_event_id: str | None = None,
        confidence: float = 0.5,
        lineage: str | None = None,
        pinned: bool = False,
    ) -> MemoryRecord:
        record = MemoryRecord(
            memory_type=memory_type,
            lifecycle_state=lifecycle_state,
            content=content,
            source_event_id=source_event_id,
            confidence=confidence,
            lineage=lineage,
            pinned=pinned,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self._db.add(record)
        return record

    def update_state(self, memory_id: str, lifecycle_state: str) -> MemoryRecord | None:
        record = self._db.get(MemoryRecord, memory_id)
        if record:
            record.lifecycle_state = lifecycle_state
        return record

    def get_active(self) -> Sequence[MemoryRecord]:
        return (
            self._db.query(MemoryRecord)
            .filter(MemoryRecord.lifecycle_state == "active")
            .order_by(MemoryRecord.updated_at.desc())
            .all()
        )

    def list_all(self) -> Sequence[MemoryRecord]:
        return (
            self._db.query(MemoryRecord)
            .order_by(MemoryRecord.updated_at.desc())
            .limit(100)
            .all()
        )

    def get_by_id(self, memory_id: str) -> MemoryRecord | None:
        return self._db.get(MemoryRecord, memory_id)
