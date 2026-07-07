import uuid
from typing import Optional

from sqlalchemy.orm import Session

from aiive.db.models import Thread, Event


class ThreadState:
    def __init__(self, db: Session):
        self._db = db

    def get_or_create_thread(self, thread_id: str | None = None) -> Thread:
        if thread_id:
            thread = self._db.get(Thread, thread_id)
            if thread:
                return thread

        thread = Thread(id=str(uuid.uuid4()))
        self._db.add(thread)
        self._db.flush()
        return thread

    def get_recent_messages(self, thread_id: str, limit: int = 20) -> list[dict]:
        events = (
            self._db.query(Event)
            .filter(
                Event.thread_id == thread_id,
                Event.event_type.in_(["user_message", "llm_response"]),
            )
            .order_by(Event.created_at.asc())
            .limit(limit)
            .all()
        )

        messages: list[dict] = []
        for event in events:
            role = "user" if event.event_type == "user_message" else "assistant"
            content = event.payload.get("content", "")
            if content:
                messages.append({"role": role, "content": content})
        return messages
