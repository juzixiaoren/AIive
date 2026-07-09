from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.db.models import OutboxJob

router = APIRouter(prefix="/api/outbox")


@router.get("/jobs")
def list_jobs(
    trace_id: str | None = Query(None),
    status: str | None = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(OutboxJob)
    if trace_id:
        q = q.filter(OutboxJob.trace_id == trace_id)
    if status:
        q = q.filter(OutboxJob.status == status)
    jobs = q.order_by(OutboxJob.created_at.desc()).limit(50).all()
    return [
        {
            "id": j.id,
            "operation_id": j.operation_id,
            "job_type": j.job_type,
            "status": j.status,
            "trace_id": j.trace_id,
            "retry_count": j.retry_count,
            "error_message": j.error_message,
            "created_at": j.created_at.isoformat(),
        }
        for j in jobs
    ]
