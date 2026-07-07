import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from aiive.db.models import Event, LLMCall


class EventLogger:
    def __init__(self, db: Session):
        self._db = db

    def log_event(
        self,
        trace_id: str,
        thread_id: str,
        event_type: str,
        payload: dict | None = None,
    ) -> Event:
        event = Event(
            id=str(uuid.uuid4()),
            trace_id=trace_id,
            thread_id=thread_id,
            event_type=event_type,
            payload=payload or {},
            created_at=datetime.now(timezone.utc),
        )
        self._db.add(event)
        self._db.flush()
        return event

    def log_llm_call(
        self,
        trace_id: str,
        thread_id: str,
        model: str,
        latency_ms: float,
        input_preview: str | None = None,
        output_preview: str | None = None,
    ) -> LLMCall:
        call = LLMCall(
            id=str(uuid.uuid4()),
            trace_id=trace_id,
            thread_id=thread_id,
            model=model,
            latency_ms=latency_ms,
            input_preview=input_preview,
            output_preview=output_preview,
            created_at=datetime.now(timezone.utc),
        )
        self._db.add(call)
        self._db.flush()
        return call
