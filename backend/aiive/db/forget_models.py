"""Phase 6A Forget Saga ORM 模型。

10 张表:
  - ForgetOperation         (Saga 根)
  - ForgetSelectorManifest  (不可变规范化 selector)
  - ForgetShield            (选择器级 + 实体级即时屏蔽)
  - ForgetTarget            (有界批次确定性物化 Target)
  - ForgetDependency        (由 Target 确定性推导的依赖)
  - ForgetBatch             (stage cursor 表，复合游标)
  - ForgetAction            (每个具体清理动作，幂等)
  - ForgetTombstone         (fail-closed + 防重抽 + 内容指纹 + 可审计性)
  - ForgetStageRun          (Stage <-> Outbox 关联，原子 deadletter)
  - ContentProvenanceRef    (规范化逐行引用)
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, _utcnow, _new_uuid  # pyright: ignore[reportPrivateUsage]


# ── 工具常量 ──

FORGET_MODES = frozenset({"memory_only", "history_only", "everywhere"})
FORGET_STATUSES = frozenset({
    "requested", "shielded", "cascading", "verifying",
    "purge_ready", "purging", "purged",
    "verified", "verified_with_coarse_purge", "legacy_unverifiable",
    "failed_retryable", "deadletter", "shielded_deadletter",
})

# 已成功完成验证、可被 retention 物理清理的 ForgetOperation 终态集合
FORGET_CLEANUP_READY_STATUSES = frozenset({
    "purged", "verified", "verified_with_coarse_purge", "legacy_unverifiable",
})
ACTION_STATUSES = frozenset({"pending", "running", "done", "skipped", "failed"})
TARGET_TYPES = frozenset({
    "memory_record", "thread", "turn_record", "event",
    "segment", "segment_summary", "epoch", "epoch_checkpoint",
    "retrieval_entry", "proposal", "working_state",
})


# ═══════════════════════════════════════════════════════════════════
# 1. ForgetOperation
# ═══════════════════════════════════════════════════════════════════

class ForgetOperation(Base):
    """遗忘操作的 Saga 根记录。

    每份用户 forget 请求创建一个 Operation；operation_key 用于幂等去重，
    跨重试/接管复用。异步阶段由 OutboxJob + ForgetStageRun 驱动。
    """
    __tablename__: str = "forget_operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    operation_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    selector_type: Mapped[str] = mapped_column(String(32), nullable=False)
    selector_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="requested",
    )
    requested_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_count: Mapped[int] = mapped_column(Integer, default=0)
    shielded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    legacy_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )

    __table_args__: tuple[CheckConstraint, ...] = (
        CheckConstraint(
            status.in_([
                "requested", "shielded", "cascading", "verifying",
                "purge_ready", "purging", "purged",
                "verified", "verified_with_coarse_purge", "legacy_unverifiable",
                "failed_retryable", "deadletter", "shielded_deadletter",
            ]),
            name="ck_forget_op_status",
        ),
    )


# ═══════════════════════════════════════════════════════════════════
# 2. ForgetSelectorManifest
# ═══════════════════════════════════════════════════════════════════

class ForgetSelectorManifest(Base):
    """不可变规范化 selector。

    Phase A 冻结 selector_payload（IDs/scope/时间范围/canonical_key 等
    结构化条件，不保存匹配原文）与 cutoff。Phase B 只能从该 payload 物化 Target。
    """
    __tablename__: str = "forget_selector_manifests"
    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint(
            "forget_operation_id", "selector_payload_hash",
            name="uq_selector_manifest_op_payload",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    forget_operation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("forget_operations.id"), nullable=False,
    )
    selector_type: Mapped[str] = mapped_column(String(32), nullable=False)
    selector_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    selector_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cutoff_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# ═══════════════════════════════════════════════════════════════════
# 3. ForgetShield
# ═══════════════════════════════════════════════════════════════════

class ForgetShield(Base):
    """选择器级 + 实体级即时屏蔽。

    所有读取路径先检查 Shield（选择器级按 thread/time/scope/all_user_data，
    实体级按 target_type/target_id），再检查逐条 Tombstone。
    仅 bounded explicit IDs（<=max_inline_shield_targets）在 Phase A 写实体级 Shield；
    canonical_key/thread/time_range/scope/all_user_data/超限 ID 只写选择器 Shield。
    """
    __tablename__: str = "forget_shields"
    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint(
            "forget_operation_id", "normalized_shield_key",
            name="uq_shield_op_norm_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    forget_operation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("forget_operations.id"), nullable=False,
    )
    selector_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    canonical_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    value_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    time_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    time_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scope_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scope_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    all_user_data: Mapped[bool] = mapped_column(Boolean, default=False)
    cutoff_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    normalized_shield_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# ═══════════════════════════════════════════════════════════════════
# 4. ForgetTarget
# ═══════════════════════════════════════════════════════════════════

class ForgetTarget(Base):
    """有界批次确定性物化 Target，冻结后不可变。

    Phase B 从 selector_payload 物化；写入后即冻，重试不重新发现或扩大。
    """
    __tablename__: str = "forget_targets"
    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint(
            "forget_operation_id", "target_type", "target_id",
            name="uq_target_op_type_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    forget_operation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("forget_operations.id"), nullable=False,
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scope_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scope_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    canonical_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    value_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    batch_no: Mapped[int] = mapped_column(Integer, default=0)
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# ═══════════════════════════════════════════════════════════════════
# 5. ForgetDependency
# ═══════════════════════════════════════════════════════════════════

class ForgetDependency(Base):
    """由 Target 确定性推导的依赖。

    仅基于已冻结 Target + 真实 provenance 推导，不再次扫描全表。
    """
    __tablename__: str = "forget_dependencies"
    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint(
            "forget_operation_id", "dependency_type", "dependency_id",
            name="uq_dep_op_type_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    forget_operation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("forget_operations.id"), nullable=False,
    )
    target_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("forget_targets.id"), nullable=True,
    )
    dependency_type: Mapped[str] = mapped_column(String(32), nullable=False)
    dependency_id: Mapped[str] = mapped_column(String(36), nullable=False)
    discovery_batch_no: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# ═══════════════════════════════════════════════════════════════════
# 6. ForgetBatch
# ═══════════════════════════════════════════════════════════════════

class ForgetBatch(Base):
    """Stage cursor 表，复合游标续跑。

    不同 lane 保存对应的（时间, sequence, ID）复合游标，禁止用数据库 offset。
    崩溃后从 cursor_start_json 续跑，无跳过、无重复。
    """
    __tablename__: str = "forget_batches"
    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint(
            "forget_operation_id", "stage", "dependency_type", "batch_no",
            name="uq_batch_op_stage_type_no",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    forget_operation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("forget_operations.id"), nullable=False,
    )
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    dependency_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    batch_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cursor_lane: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cursor_start_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    cursor_end_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )


# ═══════════════════════════════════════════════════════════════════
# 7. ForgetAction
# ═══════════════════════════════════════════════════════════════════

class ForgetAction(Base):
    """每个具体清理动作，idempotency_key 幂等。

    action_type: shield|delete_evidence|scrub_proposal|rebuild_summary|
    rebuild_checkpoint|recompute_evidence|scrub_entry|delete_token|
    scrub_maintenance_snapshot|scrub_working_state|scrub_raw_event|
    scrub_raw_turn|physical_delete|verify
    """
    __tablename__: str = "forget_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    forget_operation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("forget_operations.id"), nullable=False,
    )
    target_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("forget_targets.id"), nullable=True,
    )
    dependency_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("forget_dependencies.id"), nullable=True,
    )
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    batch_no: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    expected_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


# ═══════════════════════════════════════════════════════════════════
# 8. ForgetTombstone
# ═══════════════════════════════════════════════════════════════════

class ForgetTombstone(Base):
    """Fail-closed 屏蔽 + 防重抽 + 内容指纹 + 可审计性。

    block_visibility:   读取路径 fail-closed 屏蔽
    block_reingestion:  拦截旧来源重新抽取
    content_purged:     内容已不可逆清除，任何模式不可见
    allow_audit_read:   是否允许经授权审计绕过可见性

    审计可见性规则: allow_raw_history && allow_audit_read && !content_purged
    """
    __tablename__: str = "forget_tombstones"
    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint(
            "forget_operation_id", "target_type", "target_id",
            name="uq_tombstone_op_type_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    forget_operation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("forget_operations.id"), nullable=False,
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    canonical_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    scope_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scope_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    value_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fingerprint_key_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    block_visibility: Mapped[bool] = mapped_column(Boolean, default=True)
    block_reingestion: Mapped[bool] = mapped_column(Boolean, default=True)
    content_purged: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_audit_read: Mapped[bool] = mapped_column(Boolean, default=False)
    source_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_turn_record_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ═══════════════════════════════════════════════════════════════════
# 9. ForgetStageRun
# ═══════════════════════════════════════════════════════════════════

class ForgetStageRun(Base):
    """Stage <-> Outbox 关联，原子 deadletter。

    每个 stage 一个 OutboxJob，强关联。
    Deadletter 时: ForgetOperation.status=shielded_deadletter,
    ForgetStageRun.status=deadletter, ForgetShield.status 保持 active。
    """

    __tablename__: str = "forget_stage_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    forget_operation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("forget_operations.id"), nullable=False,
    )
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    outbox_job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("outbox_jobs.id"), nullable=False, unique=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    execution_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )


# ═══════════════════════════════════════════════════════════════════
# 10. ContentProvenanceRef
# ═══════════════════════════════════════════════════════════════════

class ContentProvenanceRef(Base):
    """规范化逐行引用。

    供 ForgetVerifier 按 source_type+source_id 有界反查并定位需 scrub 的副本。
    """

    __tablename__: str = "content_provenance_refs"
    __table_args__: tuple[UniqueConstraint | Index, ...] = (
        UniqueConstraint(
            "owner_type", "owner_id", "source_type", "source_id",
            name="uq_provenance_owner_source",
        ),
        Index("ix_provenance_source", "source_type", "source_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    owner_type: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# ── __all__ ──

__all__ = [
    "ForgetOperation",
    "ForgetSelectorManifest",
    "ForgetShield",
    "ForgetTarget",
    "ForgetDependency",
    "ForgetBatch",
    "ForgetAction",
    "ForgetTombstone",
    "ForgetStageRun",
    "ContentProvenanceRef",
    "FORGET_MODES",
    "FORGET_STATUSES",
    "ACTION_STATUSES",
    "TARGET_TYPES",
]
