"""
Phase 6B 数据保留治理模型。

定义 RetentionCleanupRun（保留清理运行记录）与 RetentionCleanupBatch（有界批次）。
Run 与 OutboxJob 1:1 关联，通过 operation_id 含 window_bucket 实现周期幂等。
Batch 使用 lane + 复合游标续跑，禁止 offset。
"""
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aiive.db.models import Base, _new_uuid, _utcnow  # pyright: ignore[reportPrivateUsage]


class RetentionCleanupRun(Base):
    """保留清理运行记录。

    一个 `retention_cleanup` OutboxJob 固定对应一个 Run。
    operation_id = retention:all:{policy_version}:{window_bucket}，支持周期可重复创建，
    同一 window_bucket 重复调度因 UNIQUE 约束仍幂等。
    current_lane 记录当前推进的 lane，一个 Run 覆盖全部 lane。
    """

    __tablename__: str = "retention_cleanup_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    outbox_job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("outbox_jobs.id"), nullable=False,
    )
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_version: Mapped[int] = mapped_column(nullable=False, default=1)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    execution_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_count: Mapped[int] = mapped_column(default=0)
    failure_count: Mapped[int] = mapped_column(default=0)
    current_lane: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scanned_count: Mapped[int] = mapped_column(default=0)
    scrubbed_count: Mapped[int] = mapped_column(default=0)
    deleted_count: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )

    __table_args__: tuple[UniqueConstraint | Index, ...] = (
        UniqueConstraint("outbox_job_id", name="uq_retention_run_outbox_job"),
        UniqueConstraint("operation_id", name="uq_retention_run_operation"),
        Index("ix_retention_run_status", "status"),
    )


class RetentionCleanupBatch(Base):
    """保留清理有界批次。

    一个 Run 内的有界处理单元，按 lane 分桶，使用复合游标（时间 + sequence + ID）续跑。
    禁止使用数据库 offset。
    """

    __tablename__: str = "retention_cleanup_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("retention_cleanup_runs.id"), nullable=False,
    )
    lane: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_no: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cursor_start_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cursor_end_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    scanned_count: Mapped[int] = mapped_column(default=0)
    scrubbed_count: Mapped[int] = mapped_column(default=0)
    deleted_count: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )

    __table_args__: tuple[UniqueConstraint | Index, ...] = (
        UniqueConstraint("run_id", "lane", "batch_no", name="uq_retention_batch_run_lane_no"),
        Index("ix_retention_batch_run_status", "run_id", "status"),
    )
