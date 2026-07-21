"""
Phase 6B: retention_cleanup OutboxJob Handler。

处理流程：
1. 从 ClaimedJob 提取 run_id 或创建新 Run
2. 使用 RetentionEngine 执行 lane 清理
3. 返回 HandlerResult（COMPLETED/CONTINUE/错误）
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from aiive.db.base import SessionLocal
from aiive.db.retention_models import RetentionCleanupRun
from aiive.retention.config import RETENTION_POLICY_V1
from aiive.retention.engine import RetentionEngine
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome, HandlerResult

logger = logging.getLogger(__name__)

MAX_POLL_BATCHES = 50  # 单次 poll 最多处理批次数


def handle_retention_cleanup(claimed: ClaimedJob) -> HandlerResult:
    """处理 retention_cleanup OutboxJob。

    每次调用处理最多 MAX_POLL_BATCHES 个 lane/批次，之后返回 CONTINUE。
    所有 lane 完成时返回 COMPLETED。
    """
    db: Session = SessionLocal()
    try:
        operation_id = claimed.payload.get("operation_id", "")
        if not operation_id:
            return HandlerResult(
                outcome=HandlerOutcome.NON_RETRYABLE,
                reason="payload 缺少 operation_id",
            )

        run = RetentionEngine.start_or_resume(
            db, claimed.id, operation_id,
        )
        db.flush()

        engine = RetentionEngine(db, run, RETENTION_POLICY_V1)

        # 分批次处理：每次最多 MAX_POLL_BATCHES 次 lane 内循环
        for _ in range(MAX_POLL_BATCHES):
            result = engine.execute()

            # 提交当前批次
            db.commit()

            if result.outcome == HandlerOutcome.COMPLETED:
                _finalize_run(db, run)
                db.commit()
                return result
            if result.outcome == HandlerOutcome.RETRYABLE_ERROR:
                return result
            if result.outcome == HandlerOutcome.CONTINUE:
                # 刷新 session 以继续
                db.expire_all()
                continue

        # 达到单次 poll 上限，返回 CONTINUE
        return HandlerResult(
            outcome=HandlerOutcome.CONTINUE,
            reason=f"单次 poll 达到 {MAX_POLL_BATCHES} 批上限",
        )
    except Exception as e:
        db.rollback()
        logger.exception("retention_cleanup 处理异常: %s", e)
        return HandlerResult(
            outcome=HandlerOutcome.RETRYABLE_ERROR,
            reason=str(e),
        )
    finally:
        db.close()


def _finalize_run(db: Session, run: RetentionCleanupRun) -> None:
    """标记 Run 完成并写入终态时间到关联 OutboxJob。"""
    from aiive.db.models import OutboxJob

    run.status = "done"
    job = db.get(OutboxJob, run.outbox_job_id)
    if job and job.terminal_at is None:
        job.terminal_at = datetime.now(timezone.utc)
    db.flush()
