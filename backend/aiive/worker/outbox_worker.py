"""Phase 0.5B: OutboxWorker with claim/lease/fencing, HandlerResult dispatch."""

from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from aiive.db.base import SessionLocal
from aiive.db.models import (
    CheckpointRun,
    CompactionRun,
    Event,
    MemoryIngestionRun,
    MemoryMaintenanceAction,
    MemoryMaintenanceBatch,
    MemoryMaintenanceRun,
    OutboxJob,
    Task,
    Thread,
)
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

    def claim_one(self, types: frozenset[str] | None = None) -> ClaimedJob | None:
        """原子 claim 一个 pending 或 lease 过期的 Job。

        types 用于在单实例 Worker 内对作业类型分片（例如 tool_operation
        单独一条 poll 线程处理，避免被 reminder_delivery 等长阻塞 turn 饿死）。
        为 None 时退化为全部 enabled_types。
        """
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            allowed = types if types is not None else self._enabled_types
            job = (
                db.query(OutboxJob)
                .filter(
                    OutboxJob.job_type.in_(allowed),
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

    def poll(
        self,
        max_jobs: int = MAX_JOBS_PER_POLL,
        only_types: frozenset[str] | None = None,
    ) -> int:
        """单次 poll：claim 并分派最多 max_jobs 个 Job。

        only_types 限定本次 poll 只处理指定 job_type（分片调度用），
        例如 tool_operation 走独立 poll 线程，避免主 poll 被长阻塞 turn 占用时
        嵌套副作用工具提交被饿死。
        """
        processed = 0
        for _ in range(max_jobs):
            claimed = self.claim_one(only_types)
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
        elif result.outcome == HandlerOutcome.CONTINUE:
            self._continue_later(claimed, result.reason)
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

    @staticmethod
    def _maintenance_report_payload(
        db: Session,
        job: OutboxJob,
        status: str,
        error: str = "",
    ) -> dict[str, Any] | None:
        """从真实维护 Run 聚合可持久化、可实时推送的终态卡片。"""
        payload = job.payload or {}
        thread_id = str(payload.get("thread_id", "") or "")
        turn_id = str(payload.get("turn_id", "") or "")
        trace_id = str(payload.get("trace_id", "") or job.trace_id or job.id)
        if not thread_id or not turn_id:
            return None
        run = db.query(MemoryMaintenanceRun).filter(
            MemoryMaintenanceRun.outbox_job_id == job.id,
        ).first()
        action_counts: dict[str, int] = {}
        if run is not None:
            rows = db.query(
                MemoryMaintenanceAction.action_type,
                func.count(MemoryMaintenanceAction.id),
            ).filter(
                MemoryMaintenanceAction.run_id == run.id,
                MemoryMaintenanceAction.status == "applied",
            ).group_by(MemoryMaintenanceAction.action_type).all()
            action_counts = {str(action_type): int(count) for action_type, count in rows}
        terminal_error = error or (run.error_message if run is not None else "") or ""
        return {
            "operation_id": job.operation_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "trace_id": trace_id,
            "status": status,
            "card": {
                "card_type": "maintenance_report",
                "title": "记忆维护已完成" if status == "completed" else "记忆维护失败",
                "summary": (
                    "后台维护已按真实运行记录完成"
                    if status == "completed"
                    else "后台维护未完成，请查看失败原因"
                ),
                "trace_id": trace_id,
                "event_ids": [],
                "resource_refs": {"operation_id": job.operation_id},
                "status": status,
                "payload_preview": {
                    "maintenance_operation_id": job.operation_id,
                    "result": {
                        "run_id": run.id if run is not None else "",
                        "candidate_count": run.candidate_count if run is not None else 0,
                        "applied_count": run.applied_count if run is not None else 0,
                        "skipped_stale_count": run.skipped_stale_count if run is not None else 0,
                        "action_counts": action_counts,
                        "error": terminal_error,
                    },
                },
                "reminder_id": "",
                "actions": [],
            },
        }

    @staticmethod
    def _persist_maintenance_report(db: Session, report: dict[str, Any]) -> None:
        """在维护终态事务中追加卡片事实，供历史加载折叠。"""
        db.add(Event(
            id=str(_uuid.uuid4()),
            trace_id=str(report["trace_id"]),
            thread_id=str(report["thread_id"]),
            event_type="maintenance_report_terminal",
            turn_id=str(report["turn_id"]),
            payload={
                "operation_id": report["operation_id"],
                "status": report["status"],
                "card": report["card"],
            },
        ))

    @staticmethod
    def _broadcast_maintenance_report(report: dict[str, Any] | None) -> None:
        """事务提交后按来源线程广播真实维护终态。"""
        if report is None:
            return
        try:
            from aiive.api.ws_manager import ws_manager

            ws_manager.broadcast_to_thread_sync(
                str(report["thread_id"]), "maintenance_report", report,
            )
        except Exception:
            logger.exception("推送记忆维护终态失败: operation_id=%s", report["operation_id"])

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
                OutboxJob.terminal_at: datetime.now(timezone.utc),
                OutboxJob.updated_at: datetime.now(timezone.utc),
            }, synchronize_session=False)
            if affected != 1:
                raise FencingViolationError(
                    f"finalize fencing: job_id={claimed.id}"
                )
            # Phase 6A: forget_* Job completed → 更新 ForgetStageRun
            if claimed.job_type.startswith("forget_"):
                _finalize_forget_stage(db, claimed)
            report = None
            if claimed.job_type == "memory_maintenance":
                job = db.get(OutboxJob, claimed.id)
                if job is not None:
                    report = self._maintenance_report_payload(db, job, "completed")
                    if report is not None:
                        self._persist_maintenance_report(db, report)
            db.commit()
            self._broadcast_maintenance_report(report)
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

    def _continue_later(self, claimed: ClaimedJob, reason: str) -> None:
        """正常分页 CONTINUE：同 Job 重新 pending、清 claim/lease、next_attempt_at=now。

        不增加 retry_count / failure_attempt_count / error / deadletter 计数；
        MemoryMaintenanceRun 保持 running（第 2/8 点）。
        """
        db = SessionLocal()
        try:
            affected = db.query(OutboxJob).filter(
                OutboxJob.id == claimed.id,
                OutboxJob.claim_token == claimed.claim_token,
                OutboxJob.locked_by == claimed.worker_id,
                OutboxJob.status == "running",
            ).update({
                OutboxJob.status: "pending",
                OutboxJob.error_message: reason[:500] if reason else None,
                OutboxJob.locked_by: None,
                OutboxJob.claim_token: None,
                OutboxJob.lease_expires_at: None,
                OutboxJob.available_at: datetime.now(timezone.utc),
                OutboxJob.updated_at: datetime.now(timezone.utc),
            }, synchronize_session=False)
            if affected != 1:
                raise FencingViolationError(
                    f"continue fencing: job_id={claimed.id}"
                )
            db.commit()
        except FencingViolationError:
            db.rollback()
            logger.warning("continue fencing violation: job_id=%s", claimed.id)
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
            job.terminal_at = datetime.now(timezone.utc)
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

            # Phase 3 Run（CompactionRun / CheckpointRun）与 OutboxJob 须处于同一终态
            # （F 节原子契约）：其 outbox_job_id 即本 Job 的 id，按类型定位并原子置 deadletter。
            if job.job_type in ("segment_sealing", "epoch_checkpoint"):
                for run_cls in (CompactionRun, CheckpointRun):
                    run = db.query(run_cls).filter(
                        run_cls.outbox_job_id == job.id,
                    ).with_for_update().first()
                    if run is not None and run.status in ("running", "failed"):
                        run.status = "deadletter"
                        run.execution_token = None
                        run.error_message = f"Outbox deadletter: {error[:200]}"
                        run.completed_at = datetime.now(timezone.utc)

            # Phase 4：MemoryMaintenanceRun 与 OutboxJob 原子 deadletter（F 节原子契约）。
            # 其未完成 Batch 统一置 `deadletter`（禁止混用 aborted+abort_reason，第 8 点）。
            if job.job_type == "memory_maintenance":
                mrun = db.query(MemoryMaintenanceRun).filter(
                    MemoryMaintenanceRun.outbox_job_id == job.id,
                ).with_for_update().first()
                if mrun is not None and mrun.status in ("running", "failed"):
                    mrun.status = "deadletter"
                    mrun.execution_token = None
                    mrun.error_message = f"Outbox deadletter: {error[:200]}"
                    mrun.completed_at = datetime.now(timezone.utc)
                    db.query(MemoryMaintenanceBatch).filter(
                        MemoryMaintenanceBatch.run_id == mrun.id,
                        MemoryMaintenanceBatch.status.in_(["frozen", "planned", "applying"]),
                    ).update({
                        MemoryMaintenanceBatch.status: "deadletter",
                        MemoryMaintenanceBatch.completed_at: datetime.now(timezone.utc),
                    }, synchronize_session=False)

            # 提醒重试耗尽时，OutboxJob 与 Task 在同一事务进入失败终态。
            if job.job_type == "reminder_delivery":
                task_id = str((job.payload or {}).get("task_id", ""))
                task = db.query(Task).filter(Task.id == task_id).with_for_update().first()
                if task is not None and task.status == "dispatching":
                    task.status = "failed"
                    task.last_checked_at = datetime.now(timezone.utc)
                    audit_thread_id = task.thread_id or "system"
                    if db.get(Thread, audit_thread_id) is None:
                        audit_thread_id = "system"
                    db.add(Event(
                        trace_id=job.trace_id or task.id,
                        thread_id=audit_thread_id,
                        event_type="reminder_delivery_failed",
                        payload={
                            "task_id": task.id,
                            "title": task.title,
                            "terminal_reason": terminal_reason or "deadletter",
                            "error": error[:500],
                        },
                    ))

            # Phase 5：retrieval_index_rebuild Job deadletter 时原子置 RetrievalIndexRun 为 deadletter
            # （F 节原子契约：其 outbox_job_id 即本 Job 的 id）。
            if job.job_type == "retrieval_index_rebuild":
                from aiive.db.models import RetrievalIndexRun
                rirun = db.query(RetrievalIndexRun).filter(
                    RetrievalIndexRun.outbox_job_id == job.id,
                ).with_for_update().first()
                if rirun is not None and rirun.status in ("running", "failed"):
                    rirun.status = "deadletter"
                    rirun.execution_token = None
                    rirun.error_message = f"Outbox deadletter: {error[:200]}"
                    rirun.completed_at = datetime.now(timezone.utc)

            # Phase 6B：retention_cleanup Job deadletter 时原子置 RetentionCleanupRun 为 deadletter
            # （F 节原子契约：其 outbox_job_id 即本 Job 的 id）。
            if job.job_type == "retention_cleanup":
                from aiive.db.retention_models import RetentionCleanupRun
                rrun = db.query(RetentionCleanupRun).filter(
                    RetentionCleanupRun.outbox_job_id == job.id,
                ).with_for_update().first()
                if rrun is not None and rrun.status in ("running", "failed"):
                    rrun.status = "deadletter"
                    rrun.execution_token = None
                    rrun.error_message = f"Outbox deadletter: {error[:200]}"
                    rrun.updated_at = datetime.now(timezone.utc)

            # Phase 6A：forget_* Job deadletter 时原子更新 ForgetStageRun + ForgetOperation + ForgetShield
            if job.job_type.startswith("forget_"):
                _deadletter_forget_stage(db, job, error)

            report = None
            if job.job_type == "memory_maintenance":
                report = self._maintenance_report_payload(db, job, "failed", error)
                if report is not None:
                    self._persist_maintenance_report(db, report)
            db.commit()
            self._broadcast_maintenance_report(report)
        except Exception:
            db.rollback()
            logger.exception("_deadletter_job_and_ingestion_run DB error: job_id=%s", claimed.id)
            raise
        finally:
            db.close()


# ═══════════════════════════════════════════════════════════════════
# Phase 6A: Forget stage 原子更新 helpers
# ═══════════════════════════════════════════════════════════════════


def _deadletter_forget_stage(db: Session, job: OutboxJob, error: str) -> None:
    """原子 deadletter：ForgetStageRun + ForgetOperation（Shield 保持 active）。"""
    from aiive.db.forget_models import ForgetOperation, ForgetStageRun

    srun = db.query(ForgetStageRun).filter(
        ForgetStageRun.outbox_job_id == job.id,
    ).with_for_update().first()
    if srun is None:
        return

    srun.status = "deadletter"
    srun.failure_count = (srun.failure_count or 0) + 1
    srun.execution_token = None
    srun.updated_at = datetime.now(timezone.utc)

    op = db.query(ForgetOperation).filter(
        ForgetOperation.id == srun.forget_operation_id,
    ).with_for_update().first()
    if op is not None and op.status != "purged":
        op.status = "shielded_deadletter"
        op.error_message = f"Stage {srun.stage} deadletter: {error[:400]}"
        op.updated_at = datetime.now(timezone.utc)

    # Shield 保持 active（不弱化）


def _finalize_forget_stage(db: Session, claimed: ClaimedJob) -> None:
    """forget_* Job completed → ForgetStageRun 标记 done。"""
    from aiive.db.forget_models import ForgetStageRun

    srun = db.query(ForgetStageRun).filter(
        ForgetStageRun.outbox_job_id == claimed.id,
    ).with_for_update().first()
    if srun is None:
        return

    srun.status = "done"
    srun.execution_token = None
    srun.updated_at = datetime.now(timezone.utc)
