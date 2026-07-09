from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.orm import Session

from aiive.db.models import OutboxJob

MAX_RETRIES = 3


class OutboxWorker:
    def __init__(self, db_session_factory: Callable[[], Session]):
        self._factory = db_session_factory
        self._handlers: dict[str, Callable] = {}

    def register_handler(self, job_type: str, handler: Callable) -> None:
        self._handlers[job_type] = handler

    def enqueue(
        self, db: Session, job_type: str, payload: dict,
        trace_id: str | None = None, operation_id: str | None = None,
    ) -> OutboxJob:
        import uuid

        op_id = operation_id or f"{job_type}:{uuid.uuid4()}"
        job = OutboxJob(
            operation_id=op_id,
            job_type=job_type,
            status="pending",
            payload=payload,
            trace_id=trace_id,
            max_retries=MAX_RETRIES,
        )
        db.add(job)
        return job

    def process_one(self, db: Session) -> int:
        job = (
            db.query(OutboxJob)
            .filter(OutboxJob.status == "pending")
            .order_by(OutboxJob.created_at.asc())
            .with_for_update(skip_locked=True)
            .first()
        )
        if not job:
            return 0

        handler = self._handlers.get(job.job_type)
        if not handler:
            job.status = "deadletter"
            job.error_message = f"No handler for job_type: {job.job_type}"
            return 1

        job.status = "running"
        db.flush()

        try:
            handler(db, job.payload, job.trace_id)
            job.status = "completed"
        except Exception as e:
            job.retry_count += 1
            job.error_message = str(e)[:500]
            if job.retry_count >= job.max_retries:
                job.status = "deadletter"
            else:
                job.status = "pending"
        finally:
            job.updated_at = datetime.now(timezone.utc)
            db.flush()

        return 1

    def process_all(self, db: Session, max_jobs: int = 10) -> int:
        processed = 0
        for _ in range(max_jobs):
            if self.process_one(db) == 0:
                break
            processed += 1
        return processed
