"""
API路由模块：发件箱（Outbox）任务管理
- 提供发件箱任务的查询接口
- 支持按 trace_id 和 status 过滤
"""
import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from aiive.api.developer_security import redact_diagnostic_text, require_local_developer
from aiive.db.base import get_db
from aiive.db.models import OutboxJob

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/outbox", dependencies=[Depends(require_local_developer)])


@router.get("/jobs")
def list_jobs(
    trace_id: str | None = Query(None),
    status: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """查询发件箱任务列表

    支持按 trace_id 和状态进行过滤。

    Args:
        trace_id: 追踪ID（可选）
        status: 任务状态（可选），如 pending、completed、failed
        db: 数据库会话

    Returns:
        发件箱任务列表，按创建时间降序排列，最多50条
    """
    try:
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
                "error_message": redact_diagnostic_text(j.error_message),
                "created_at": j.created_at.isoformat(),
            }
            for j in jobs
        ]
    except Exception:
        logger.exception("查询发件箱任务列表失败")
        raise
