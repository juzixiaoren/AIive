from datetime import datetime, timezone

from sqlalchemy.orm import Session

from aiive.db.models import ForgetRequest, MemoryRecord


class MemoryMaintenance:
    def __init__(self, db: Session):
        self._db = db

    def forget(self, memory_id: str, reason: str = "") -> dict:
        record = self._db.get(MemoryRecord, memory_id)
        if not record:
            return {"ok": False, "error": "Memory not found"}

        tombstone = f"forgotten:{memory_id[:8]}:{datetime.now(timezone.utc).isoformat()}"
        record.lifecycle_state = "forgotten"
        record.content = tombstone

        fr = ForgetRequest(
            memory_id=memory_id,
            reason=reason,
            tombstone=tombstone,
        )
        self._db.add(fr)
        self._db.flush()
        return {"ok": True, "memory_id": memory_id, "tombstone": tombstone}

    def sleep(self, memory_id: str) -> dict:
        record = self._db.get(MemoryRecord, memory_id)
        if not record:
            return {"ok": False, "error": "Memory not found"}
        if record.pinned:
            return {"ok": False, "error": "Cannot sleep pinned memory"}
        record.lifecycle_state = "sleep"
        return {"ok": True, "memory_id": memory_id}

    def archive(self, memory_id: str) -> dict:
        record = self._db.get(MemoryRecord, memory_id)
        if not record:
            return {"ok": False, "error": "Memory not found"}
        record.lifecycle_state = "archive"
        return {"ok": True, "memory_id": memory_id}

    def scan(self) -> dict:
        records = (
            self._db.query(MemoryRecord)
            .filter(MemoryRecord.lifecycle_state.in_(["active", "candidate"]))
            .all()
        )
        sleep_candidates = []
        archive_candidates = []

        for r in records:
            if r.pinned:
                continue
            created = r.created_at.replace(tzinfo=timezone.utc) if r.created_at.tzinfo is None else r.created_at
            age_days = (datetime.now(timezone.utc) - created).days
            if r.confidence < 0.3 and age_days > 30:
                archive_candidates.append(r.id)
            elif r.lifecycle_state == "candidate" and age_days > 7:
                sleep_candidates.append(r.id)

        return {
            "total_active": len(records),
            "sleep_candidates": len(sleep_candidates),
            "archive_candidates": len(archive_candidates),
            "sleep_ids": sleep_candidates,
            "archive_ids": archive_candidates,
        }
