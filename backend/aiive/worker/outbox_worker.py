"""Phase 0.5B: OutboxWorker with claim/lease/fencing, HandlerResult dispatch."""

from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from aiive.db.base import SessionLocal
from aiive.db.models import MemoryIngestionRun, OutboxJob
from aiive.memory.recall_config import ENABLED_OUTBOX_JOB_TYPES
from aiive.worker.handler_registry import HandlerRegistry
from aiive.worker.outbox_dto import (
    ActiveClaim,
    ClaimedJob,
    FencingViolationError,
    HandlerOutcome,
    HandlerResult,
)
from aiive.worker.outbox_heartbeat import (
    ActiveClaimRegistry,
    LEASE_DURATION,
)

logger = logging.getLogger(__name__)

MAX_JOBS_PER_POLL = 10
RETRY_BACKOFF_BASE = 2
RETRY_BACKOFF_MAX = 60


class OutboxWorker:
    """发件箱工作器：claim → 分派 Handler → 根据 HandlerResult 更新 Outbox。

    单例模式，生命周期由 main.py lifespan 管理。
    Worker 只负责 OutboxJob 的 claim/finalize/retry，不介入业务 Session。
    """

    def __init__(
        self,
        worker_id: str,
        registry: HandlerRegistry,
        claims: ActiveClaimRegistry,
    ) -> None:
        self._worker_id: str = worker_id
        self._registry: HandlerRegistry = registry
        self._claims: ActiveClaimRegistry = claims
        self._enabled_types: frozenset[str] = ENABLED_OUTBOX_JOB_TYPES

    # ------------------------------------------------------------------
    # claim_one
    # ------------------------------------------------------------------

    def claim_one(self) -> ClaimedJob | None:
        """原子 claim 一个 pending 或 lease 过期的 Job。"""
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            job = (
                db.query(OutboxJob)
                .filter(
                    OutboxJob.job_type.in_(self._enabled_types),
                    or_(
                        OutboxJob.status == "pending",
                        OutboxJob.status == "running",
                    ),
                    or_(
                        OutboxJob.available_at.is_(None),
                        OutboxJob.available_at <= now,
                    ),
                )
                .order_by(OutboxJob.created_at.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
                .first()
            )
            if job is None:
                db.rollback()
                return None

            # 如果是 running，检查 lease 是否过期
            if job.status == "running":
                if job.lease_expires_at:
                    lease = job.lease_expires_at
                    if lease.tzinfo is None:
                        lease = lease.replace(tzinfo=timezone.utc)
                    if lease > now:
                        db.rollback()
                        return None  # lease 仍有效，被其他 Worker 持有

            claim_token = str(_uuid.uuid4())
            job.status = "running"
            job.locked_by = self._worker_id
            job.claim_token = claim_token
            job.lease_expires_at = now + LEASE_DURATION
            db.flush()

            dto = ClaimedJob(
                id=job.id,
                job_type=job.job_type,
                payload=dict(job.payload or {}),
                trace_id=job.trace_id,
                retry_count=job.retry_count,
                max_retries=job.max_retries,
                claim_token=claim_token,
                schema_version=job.schema_version or 1,
                worker_id=self._worker_id,
            )
            db.commit()
            return dto
        finally:
            db.close()

    # ------------------------------------------------------------------
    # enqueue (with allowlist)
    # ------------------------------------------------------------------

    def enqueue(
        self,
        db: Session,
        job_type: str,
        payload: dict[str, Any],
        trace_id: str | None = None,
        operation_id: str | None = None,
    ) -> OutboxJob:
        """将新作业加入队列。不在 allowlist 中的 job_type 拒绝。"""
        if job_type not in self._enabled_types:
            raise ValueError(
                f"Job type '{job_type}' is not in enabled allowlist. "
                + f"Enabled: {sorted(self._enabled_types)}"
            )
        op_id = operation_id or f"{job_type}:{_uuid.uuid4()}"
        job = OutboxJob(
            operation_id=op_id,
            job_type=job_type,
            status="pending",
            payload=payload,
            trace_id=trace_id,
            max_retries=3,
        )
        db.add(job)
        return job

    # ------------------------------------------------------------------
    # poll: main loop
    # ------------------------------------------------------------------

    def poll(self, max_jobs: int = MAX_JOBS_PER_POLL) -> int:
        """单次 poll：claim 并分派最多 max_jobs 个 Job。"""
        processed = 0
        for _ in range(max_jobs):
            claimed = self.claim_one()
            if claimed is None:
                break

            active = ActiveClaim(
                job_id=claimed.id,
                claim_token=claimed.claim_token,
                worker_id=self._worker_id,
                started_at=datetime.now(timezone.utc),
            )
            self._claims.add(active)

            try:
                self._dispatch_one(claimed)
            finally:
                self._claims.remove(claimed.id, claimed.claim_token)
            processed += 1
        return processed

    # ------------------------------------------------------------------
    # _dispatch_one
    # ------------------------------------------------------------------

    def _dispatch_one(self, claimed: ClaimedJob) -> None:
        """分派单个 Job 到 Handler，根据 HandlerResult 更新 Outbox。"""
        handler = self._registry.get(claimed.job_type)
        if handler is None:
            self._deadletter_job_and_ingestion_run(
                claimed, "",
                error=f"No handler: {claimed.job_type}",
                terminal_reason="unknown_job_type",
            )
            return

        # Worker 统一预校验 schema_version
        if not self._registry.is_schema_supported(claimed.job_type, claimed.schema_version):
            self._deadletter_job_and_ingestion_run(
                claimed, "",
                error=f"Unsupported schema_version={claimed.schema_version} for {claimed.job_type}",
                terminal_reason="unsupported_schema_version",
            )
            return

        try:
            result: HandlerResult = handler(claimed)
        except Exception as e:
            logger.exception("Handler crashed: job_id=%s", claimed.id)
            self._retry_or_deadletter(claimed, str(e))
            return

        if result.outcome == HandlerOutcome.COMPLETED:
            self._finalize_job(claimed, result.reason)
        elif result.outcome == HandlerOutcome.RETRY_LATER:
            self._retry_later(claimed, result.retry_available_at, result.reason)
        elif result.outcome == HandlerOutcome.NON_RETRYABLE:
            self._deadletter_job_and_ingestion_run(
                claimed,
                ingestion_run_id=result.ingestion_run_id,
                error=result.reason,
                terminal_reason=result.terminal_reason,
            )
        elif result.outcome == HandlerOutcome.RETRYABLE_ERROR:
            self._retry_or_deadletter(
                claimed, result.reason,
                ingestion_run_id=result.ingestion_run_id,
                terminal_reason=result.terminal_reason,
            )
        elif result.outcome == HandlerOutcome.CLAIM_LOST:
            logger.warning(
                "Claim lost: job_id=%s token=%s",
                claimed.id, claimed.claim_token,
            )

    # ------------------------------------------------------------------
    # finalize / retry / deadletter (all with fencing)
    # ------------------------------------------------------------------

    def _finalize_job(self, claimed: ClaimedJob, reason: str) -> None:
        db = SessionLocal()
        try:
            affected = db.query(OutboxJob).filter(
                OutboxJob.id == claimed.id,
                OutboxJob.claim_token == claimed.claim_token,
                OutboxJob.locked_by == claimed.worker_id,
                OutboxJob.status == "running",
            ).update({
                OutboxJob.status: "completed",
                OutboxJob.error_message: reason[:500] if reason else None,
                OutboxJob.locked_by: None,
                OutboxJob.claim_token: None,
                OutboxJob.lease_expires_at: None,
                OutboxJob.updated_at: datetime.now(timezone.utc),
            }, synchronize_session=False)
            if affected != 1:
                raise FencingViolationError(
                    f"finalize fencing: job_id={claimed.id}"
                )
            db.commit()
        except FencingViolationError:
            db.rollback()
            logger.warning("finalize fencing violation: job_id=%s", claimed.id)
        finally:
            db.close()

    def _retry_later(
        self, claimed: ClaimedJob, available_at: datetime | None, reason: str,
    ) -> None:
        db = SessionLocal()
        try:
            affected = db.query(OutboxJob).filter(
                OutboxJob.id == claimed.id,
                OutboxJob.claim_token == claimed.claim_token,
                OutboxJob.locked_by == claimed.worker_id,
                OutboxJob.status == "running",
            ).update({
                OutboxJob.status: "pending",
                OutboxJob.error_message: reason[:500],
                OutboxJob.locked_by: None,
                OutboxJob.claim_token: None,
                OutboxJob.lease_expires_at: None,
                OutboxJob.available_at: available_at or (
                    datetime.now(timezone.utc) + timedelta(seconds=30)
                ),
                OutboxJob.updated_at: datetime.now(timezone.utc),
            }, synchronize_session=False)
            if affected != 1:
                raise FencingViolationError(
                    f"retry_later fencing: job_id={claimed.id}"
                )
            db.commit()
        except FencingViolationError:
            db.rollback()
            logger.warning("retry_later fencing violation: job_id=%s", claimed.id)
        finally:
            db.close()

    def _retry_or_deadletter(
        self,
        claimed: ClaimedJob,
        error: str,
        ingestion_run_id: str = "",
        terminal_reason: str = "",
    ) -> None:
        new_retry = claimed.retry_count + 1
        if new_retry >= claimed.max_retries:
            self._deadletter_job_and_ingestion_run(
                claimed, ingestion_run_id,
                error=error,
                terminal_reason=terminal_reason or "max_retries_exhausted",
            )
            return

        db = SessionLocal()
        try:
            backoff = min(RETRY_BACKOFF_MAX, RETRY_BACKOFF_BASE ** new_retry)
            affected = db.query(OutboxJob).filter(
                OutboxJob.id == claimed.id,
                OutboxJob.claim_token == claimed.claim_token,
                OutboxJob.locked_by == claimed.worker_id,
                OutboxJob.status == "running",
            ).update({
                OutboxJob.status: "pending",
                OutboxJob.retry_count: new_retry,
                OutboxJob.error_message: error[:500],
                OutboxJob.locked_by: None,
                OutboxJob.claim_token: None,
                OutboxJob.lease_expires_at: None,
                OutboxJob.available_at: datetime.now(timezone.utc) + timedelta(seconds=backoff),
                OutboxJob.updated_at: datetime.now(timezone.utc),
            }, synchronize_session=False)
            if affected != 1:
                raise FencingViolationError(
                    f"retry fencing: job_id={claimed.id}"
                )
            db.commit()
        except FencingViolationError:
            db.rollback()
            logger.warning("retry fencing violation: job_id=%s", claimed.id)
        finally:
            db.close()

    # ------------------------------------------------------------------
    # atomic deadletter: OutboxJob + MemoryIngestionRun
    # ------------------------------------------------------------------

    def _deadletter_job_and_ingestion_run(
        self,
        claimed: ClaimedJob,
        ingestion_run_id: str,
        error: str,
        terminal_reason: str = "",
    ) -> None:
        """原子 deadletter：OutboxJob + IngestionRun 在同一事务中。"""
        db = SessionLocal()
        try:
            job = db.query(OutboxJob).filter(
                OutboxJob.id == claimed.id,
            ).with_for_update().first()

            if job is None:
                db.rollback()
                logger.warning("_deadletter: OutboxJob not found: %s", claimed.id)
                return

            # fencing: 只有持有有效 claim 才能 deadletter
            if not (
                job.claim_token == claimed.claim_token
                and job.locked_by == claimed.worker_id
                and job.status == "running"
            ):
                db.rollback()
                logger.warning(
                    "_deadletter fencing violation: job_id=%s token=%s",
                    claimed.id, claimed.claim_token,
                )
                return

            # 更新 OutboxJob
            job.status = "deadletter"
            job.terminal_reason = terminal_reason or "deadletter"
            job.error_message = error[:500]
            job.locked_by = None
            job.claim_token = None
            job.lease_expires_at = None
            job.updated_at = datetime.now(timezone.utc)

            # 如果有关联 IngestionRun，原子标记 deadletter
            if ingestion_run_id:
                irun = db.query(MemoryIngestionRun).filter(
                    MemoryIngestionRun.id == ingestion_run_id,
                ).with_for_update().first()

                if irun is not None and irun.status in ("running", "failed"):
                    irun.status = "deadletter"
                    irun.execution_token = None
                    irun.error_message = f"Outbox deadletter: {error[:200]}"
                    irun.completed_at = datetime.now(timezone.utc)

            db.commit()
        except Exception:
            db.rollback()
            logger.exception("_deadletter_job_and_ingestion_run DB error: job_id=%s", claimed.id)
            raise
        finally:
            db.close()
