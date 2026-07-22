"""Forget Saga 后台对账与自动恢复服务。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from aiive.db.forget_models import ForgetOperation, ForgetStageRun
from aiive.db.models import OutboxJob


FORGET_STAGE_ORDER = (
    "cascade",
    "rebuild_dependencies",
    "purge",
    "verify",
)
FORGET_TERMINAL_STATUSES = frozenset({
    "purged",
    "verified",
    "verified_with_coarse_purge",
    "legacy_unverifiable",
})
_RECOVERABLE_JOB_STATUSES = frozenset({"deadletter", "failed"})
_STAGE_OPERATION_STATUSES = {
    "cascade": "shielded",
    "rebuild_dependencies": "cascading",
    "purge": "purging",
    "verify": "verifying",
}


@dataclass(frozen=True)
class ForgetReconcileResult:
    """单个遗忘操作的对账结果。"""

    action: str
    stage: str | None = None


class ForgetReconcileService:
    """检查遗忘 Saga 状态，并补发或恢复第一个不可推进的阶段。"""

    @classmethod
    def scan_once(
        cls,
        db: Session,
        *,
        limit: int = 10,
        now: datetime | None = None,
    ) -> list[tuple[str, ForgetReconcileResult]]:
        """分批扫描非终态遗忘操作，并逐个执行幂等对账。"""
        operation_ids = [
            row[0]
            for row in (
                db.query(ForgetOperation.id)
                .filter(ForgetOperation.status.notin_(FORGET_TERMINAL_STATUSES))
                .order_by(ForgetOperation.updated_at.asc(), ForgetOperation.id.asc())
                .limit(limit)
                .all()
            )
        ]
        results: list[tuple[str, ForgetReconcileResult]] = []
        for operation_id in operation_ids:
            result = cls.reconcile_operation(db, operation_id, now=now)
            results.append((operation_id, result))
        return results

    @classmethod
    def reconcile_operation(
        cls,
        db: Session,
        operation_id: str,
        *,
        now: datetime | None = None,
    ) -> ForgetReconcileResult:
        """锁定并修复一个遗忘操作；不会执行具体遗忘阶段。"""
        now = now or datetime.now(timezone.utc)
        operation = (
            db.query(ForgetOperation)
            .filter(ForgetOperation.id == operation_id)
            .with_for_update()
            .first()
        )
        if operation is None:
            return ForgetReconcileResult("operation_not_found")
        if operation.status in FORGET_TERMINAL_STATUSES:
            return ForgetReconcileResult("terminal")

        stage_runs = (
            db.query(ForgetStageRun)
            .filter(ForgetStageRun.forget_operation_id == operation_id)
            .order_by(ForgetStageRun.created_at.asc(), ForgetStageRun.id.asc())
            .all()
        )
        runs_by_stage: dict[str, ForgetStageRun] = {}
        for stage_run in stage_runs:
            if stage_run.stage in runs_by_stage:
                return ForgetReconcileResult("duplicate_stage_run", stage_run.stage)
            runs_by_stage[stage_run.stage] = stage_run

        for stage in FORGET_STAGE_ORDER:
            stage_run = runs_by_stage.get(stage)
            created_job = False
            if stage_run is None:
                stage_run, job, created_job = cls._ensure_stage(db, operation, stage, now)
            else:
                job = (
                    db.query(OutboxJob)
                    .filter(OutboxJob.id == stage_run.outbox_job_id)
                    .with_for_update()
                    .first()
                )
                if job is None:
                    return ForgetReconcileResult("outbox_job_missing", stage)

            if created_job:
                return ForgetReconcileResult("enqueued", stage)

            if job.status == "completed":
                if stage_run.status != "done":
                    stage_run.status = "done"
                    stage_run.execution_token = None
                    stage_run.updated_at = now
                if stage == "verify" and operation.status not in FORGET_TERMINAL_STATUSES:
                    if not cls._recovery_due(stage_run, now):
                        operation.updated_at = now
                        return ForgetReconcileResult("backoff", stage)
                    cls._reactivate_stage(operation, stage_run, job, stage, now)
                    return ForgetReconcileResult("reactivated", stage)
                continue

            if job.status in _RECOVERABLE_JOB_STATUSES:
                if not cls._recovery_due(stage_run, now):
                    operation.updated_at = now
                    return ForgetReconcileResult("backoff", stage)
                cls._reactivate_stage(operation, stage_run, job, stage, now)
                return ForgetReconcileResult("reactivated", stage)

            if job.status == "pending":
                if stage_run.status != "pending":
                    stage_run.status = "pending"
                    stage_run.execution_token = None
                    stage_run.updated_at = now
                operation.updated_at = now
                return ForgetReconcileResult("in_flight", stage)

            if job.status == "running":
                operation.updated_at = now
                return ForgetReconcileResult("in_flight", stage)

            return ForgetReconcileResult("unsupported_job_status", stage)

        return ForgetReconcileResult("all_stages_completed")

    @staticmethod
    def _ensure_stage(
        db: Session,
        operation: ForgetOperation,
        stage: str,
        now: datetime,
    ) -> tuple[ForgetStageRun, OutboxJob, bool]:
        """确保确定性阶段 Job 与 StageRun 同时存在。"""
        operation_id = f"forget:{operation.id}:{stage}"
        job = (
            db.query(OutboxJob)
            .filter(OutboxJob.operation_id == operation_id)
            .with_for_update()
            .first()
        )
        created_job = job is None
        if job is None:
            job = OutboxJob(
                operation_id=operation_id,
                job_type=f"forget_{stage}",
                status="pending",
                payload={"forget_operation_id": operation.id},
                max_retries=3,
                available_at=now,
            )
            db.add(job)
            db.flush()

        stage_run = ForgetStageRun(
            id=str(uuid.uuid4()),
            forget_operation_id=operation.id,
            stage=stage,
            outbox_job_id=job.id,
            status="done" if job.status == "completed" else "pending",
        )
        db.add(stage_run)
        operation.status = _STAGE_OPERATION_STATUSES[stage]
        operation.error_message = None
        operation.updated_at = now
        return stage_run, job, created_job

    @staticmethod
    def _reactivate_stage(
        operation: ForgetOperation,
        stage_run: ForgetStageRun,
        job: OutboxJob,
        stage: str,
        now: datetime,
    ) -> None:
        """复用原 Job 自动恢复失败阶段，并清除旧 claim 与终态错误。"""
        failure_count = stage_run.failure_count or 0
        stage_run.status = "pending"
        stage_run.execution_token = None
        stage_run.failure_count = failure_count + 1
        stage_run.updated_at = now

        job.status = "pending"
        job.retry_count = 0
        job.error_message = None
        job.locked_by = None
        job.claim_token = None
        job.lease_expires_at = None
        job.available_at = now
        job.terminal_reason = None
        job.updated_at = now

        operation.status = _STAGE_OPERATION_STATUSES[stage]
        operation.error_message = None
        operation.updated_at = now

    @staticmethod
    def _recovery_due(stage_run: ForgetStageRun, now: datetime) -> bool:
        """按累计失败次数退避自动恢复，最长等待一小时。"""
        updated_at = stage_run.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        failures = max(1, stage_run.failure_count or 1)
        delay_seconds = min(3600, 2 ** min(failures, 12))
        return updated_at + timedelta(seconds=delay_seconds) <= now
