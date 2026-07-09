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
        memory_key: str | None = None,
        exclusivity: str = "multi_active",
        revision_num: int = 1,
        supersedes: str | None = None,
    ) -> MemoryRecord:
        record = MemoryRecord(
            memory_type=memory_type,
            lifecycle_state=lifecycle_state,
            content=content,
            source_event_id=source_event_id,
            confidence=confidence,
            lineage=lineage,
            pinned=pinned,
            memory_key=memory_key,
            revision_num=revision_num,
            supersedes=supersedes,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self._db.add(record)
        self._db.flush()
        return record

    def update_state(self, memory_id: str, lifecycle_state: str) -> MemoryRecord | None:
        record = self._db.get(MemoryRecord, memory_id)
        if record:
            record.lifecycle_state = lifecycle_state
        return record

    def update_content(self, memory_id: str, new_content: str, reason: str = "") -> MemoryRecord | None:
        record = self._db.get(MemoryRecord, memory_id)
        if record:
            record.content = new_content
            record.updated_at = datetime.now(timezone.utc)
        return record

    def supersede(self, old_id: str, new_content: str, reason: str = "") -> MemoryRecord | None:
        old = self._db.get(MemoryRecord, old_id)
        if not old:
            return None
        old.lifecycle_state = "superseded"
        old.updated_at = datetime.now(timezone.utc)
        self._db.flush()

        new_rec = self.create(
            content=new_content,
            memory_type=old.memory_type,
            lifecycle_state="active",
            confidence=1.0,
            lineage=f"supersedes:{old_id}",
            memory_key=old.memory_key,
            revision_num=old.revision_num + 1,
            supersedes=old.id,
        )
        old.superseded_by = new_rec.id
        self._db.flush()
        return new_rec

    def get_active(self) -> Sequence[MemoryRecord]:
        return (
            self._db.query(MemoryRecord)
            .filter(MemoryRecord.lifecycle_state == "active")
            .order_by(MemoryRecord.updated_at.desc())
            .all()
        )

    def resolve_for_context(self) -> Sequence[MemoryRecord]:
        """Return only active, non-superseded records for context injection."""
        return self.get_active()

    def list_all(self) -> Sequence[MemoryRecord]:
        return (
            self._db.query(MemoryRecord)
            .order_by(MemoryRecord.updated_at.desc())
            .limit(100)
            .all()
        )

    def get_by_id(self, memory_id: str) -> MemoryRecord | None:
        return self._db.get(MemoryRecord, memory_id)
