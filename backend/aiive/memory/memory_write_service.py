"""V20 MemoryWriteService: unified memory write pipeline.

All memory writes MUST go through this service. No code may directly call
MemoryStore.create() or MemoryStore.supersede() for production paths.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from aiive.memory.memory_gate import MemoryGateDecision
from aiive.memory.memory_store import MemoryStore
from aiive.runtime.event_logger import EventLogger


class MemoryWriteService:
    def __init__(self, db: Session):
        self._db = db
        self._store = MemoryStore(db)
        self._logger = EventLogger(db)

    def write(self, decision: MemoryGateDecision, content: str) -> dict:
        """Execute the admission decision. Returns observable result."""

        if decision.decision == "reject":
            self._logger.log_event(
                trace_id=decision.trace_id or "memory",
                thread_id="",
                event_type="memory.rejected",
                payload={
                    "reason": decision.reason,
                    "blocked_reason": decision.blocked_reason,
                    "content_preview": content[:200],
                },
            )
            return {"written": False, "reason": decision.reason}

        if decision.decision == "candidate":
            record = self._store.create(
                content=content,
                memory_type=decision.memory_type or "fact",
                lifecycle_state="candidate",
                confidence=decision.confidence,
                memory_key=decision.memory_key,
                lineage=f"trace:{decision.trace_id}",
            )
            self._logger.log_event(
                trace_id=decision.trace_id or "memory",
                thread_id="",
                event_type="memory.candidate_created",
                payload={
                    "memory_id": record.id,
                    "memory_key": decision.memory_key,
                    "confidence": decision.confidence,
                },
            )
            return {"written": True, "state": "candidate", "memory_id": record.id}

        # ---- active ----
        if decision.update_mode == "supersede" and decision.supersede_memory_ids:
            old_id = decision.supersede_memory_ids[0]
            new_rec = self._store.supersede(old_id, content)
            self._logger.log_event(
                trace_id=decision.trace_id or "memory",
                thread_id="",
                event_type="memory.superseded",
                payload={
                    "old_memory_id": old_id,
                    "new_memory_id": new_rec.id if new_rec else "",
                    "memory_key": decision.memory_key,
                    "reason": decision.reason,
                },
            )
            self._logger.log_event(
                trace_id=decision.trace_id or "memory",
                thread_id="",
                event_type="memory.created",
                payload={
                    "memory_id": new_rec.id if new_rec else "",
                    "memory_key": decision.memory_key,
                    "memory_type": decision.memory_type,
                    "update_mode": "supersede",
                },
            )
            return {
                "written": True, "state": "active",
                "memory_id": new_rec.id if new_rec else "",
                "superseded_old": True,
            }

        if decision.update_mode == "upsert":
            # Find and deactivate any existing active with same key
            for old in self._store.get_active():
                if old.memory_key == decision.memory_key:
                    old.lifecycle_state = "superseded"
                    old.superseded_by = "pending"
                    old.updated_at = datetime.now(timezone.utc)

            record = self._store.create(
                content=content,
                memory_type=decision.memory_type or "fact",
                lifecycle_state="active",
                confidence=decision.confidence,
                memory_key=decision.memory_key,
                lineage=f"trace:{decision.trace_id}",
            )
            self._logger.log_event(
                trace_id=decision.trace_id or "memory",
                thread_id="",
                event_type="memory.created",
                payload={
                    "memory_id": record.id,
                    "memory_key": decision.memory_key,
                    "memory_type": decision.memory_type,
                    "update_mode": "upsert",
                },
            )
            return {"written": True, "state": "active", "memory_id": record.id}

        # create
        record = self._store.create(
            content=content,
            memory_type=decision.memory_type or "fact",
            lifecycle_state="active",
            confidence=decision.confidence,
            memory_key=decision.memory_key,
            lineage=f"trace:{decision.trace_id}",
        )
        self._logger.log_event(
            trace_id=decision.trace_id or "memory",
            thread_id="",
            event_type="memory.created",
            payload={
                "memory_id": record.id,
                "memory_key": decision.memory_key,
                "memory_type": decision.memory_type,
                "update_mode": "create",
            },
        )
        return {"written": True, "state": "active", "memory_id": record.id}
