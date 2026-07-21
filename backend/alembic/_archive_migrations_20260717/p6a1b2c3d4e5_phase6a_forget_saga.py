"""Phase 6A: Forget Saga 核心表。

新表 (10 张):
  - forget_operations       (Saga 根)
  - forget_selector_manifests (不可变规范化 selector)
  - forget_shields          (选择器级 + 实体级即时屏蔽)
  - forget_targets          (有界批次确定性物化 Target)
  - forget_dependencies     (由 Target 确定性推导的依赖)
  - forget_batches          (stage cursor 表，复合游标)
  - forget_actions          (每个具体清理动作，幂等)
  - forget_tombstones       (fail-closed + 防重抽 + 内容指纹 + 可审计性)
  - forget_stage_runs       (Stage <-> Outbox 关联，原子 deadletter)
  - content_provenance_refs (规范化逐行引用)

旧 forget_requests 表保留不动，只读兼容。

Revision ID: p6a1b2c3d4e5
Revises: d4e5f6a7b8c9
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "p6a1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type() -> "type":
    """PostgreSQL JSONB，其他方言回退到 JSON。"""
    bind = op.get_bind()
    return postgresql.JSONB if bind.dialect.name == "postgresql" else sa.JSON()


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    json_type = _json_type()

    # ── 1. forget_operations ──
    op.create_table(
        "forget_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("operation_key", sa.String(128), nullable=False, unique=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("selector_type", sa.String(32), nullable=False),
        sa.Column("selector_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'requested'")),
        sa.Column("requested_by", sa.String(36), nullable=True),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("target_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("shielded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("legacy_request_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    # ── 2. forget_selector_manifests ──
    op.create_table(
        "forget_selector_manifests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "forget_operation_id", sa.String(36),
            sa.ForeignKey("forget_operations.id"), nullable=False,
        ),
        sa.Column("selector_type", sa.String(32), nullable=False),
        sa.Column("selector_payload", json_type, nullable=False),
        sa.Column("selector_payload_hash", sa.String(64), nullable=False),
        sa.Column("cutoff_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_unique_constraint(
        "uq_selector_manifest_op_payload",
        "forget_selector_manifests",
        ["forget_operation_id", "selector_payload_hash"],
    )

    # ── 3. forget_shields ──
    op.create_table(
        "forget_shields",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "forget_operation_id", sa.String(36),
            sa.ForeignKey("forget_operations.id"), nullable=False,
        ),
        sa.Column("selector_type", sa.String(32), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(36), nullable=True),
        sa.Column("canonical_key", sa.String(256), nullable=True),
        sa.Column("value_fingerprint", sa.String(64), nullable=True),
        sa.Column("thread_id", sa.String(36), nullable=True),
        sa.Column("time_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("time_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scope_type", sa.String(32), nullable=True),
        sa.Column("scope_id", sa.String(128), nullable=True),
        sa.Column("all_user_data", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("cutoff_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'active'")),
        sa.Column("normalized_shield_key", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_unique_constraint(
        "uq_shield_op_norm_key",
        "forget_shields",
        ["forget_operation_id", "normalized_shield_key"],
    )

    # ── 4. forget_targets ──
    op.create_table(
        "forget_targets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "forget_operation_id", sa.String(36),
            sa.ForeignKey("forget_operations.id"), nullable=False,
        ),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("source_version", sa.String(64), nullable=True),
        sa.Column("source_hash", sa.String(64), nullable=True),
        sa.Column("scope_type", sa.String(32), nullable=True),
        sa.Column("scope_id", sa.String(128), nullable=True),
        sa.Column("canonical_key", sa.String(256), nullable=True),
        sa.Column("value_hash", sa.String(64), nullable=True),
        sa.Column("batch_no", sa.Integer(), server_default=sa.text("0")),
        sa.Column("frozen_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_unique_constraint(
        "uq_target_op_type_id",
        "forget_targets",
        ["forget_operation_id", "target_type", "target_id"],
    )

    # ── 5. forget_dependencies ──
    op.create_table(
        "forget_dependencies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "forget_operation_id", sa.String(36),
            sa.ForeignKey("forget_operations.id"), nullable=False,
        ),
        sa.Column(
            "target_id", sa.String(36),
            sa.ForeignKey("forget_targets.id"), nullable=True,
        ),
        sa.Column("dependency_type", sa.String(32), nullable=False),
        sa.Column("dependency_id", sa.String(36), nullable=False),
        sa.Column("discovery_batch_no", sa.Integer(), server_default=sa.text("0")),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("discovered_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_unique_constraint(
        "uq_dep_op_type_id",
        "forget_dependencies",
        ["forget_operation_id", "dependency_type", "dependency_id"],
    )

    # ── 6. forget_batches ──
    op.create_table(
        "forget_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "forget_operation_id", sa.String(36),
            sa.ForeignKey("forget_operations.id"), nullable=False,
        ),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("dependency_type", sa.String(32), nullable=True),
        sa.Column("batch_no", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cursor_lane", sa.String(32), nullable=True),
        sa.Column("cursor_start_json", json_type, nullable=True),
        sa.Column("cursor_end_json", json_type, nullable=True),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("input_hash", sa.String(64), nullable=True),
        sa.Column("action_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("completed_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_unique_constraint(
        "uq_batch_op_stage_type_no",
        "forget_batches",
        ["forget_operation_id", "stage", "dependency_type", "batch_no"],
    )

    # ── 7. forget_actions ──
    op.create_table(
        "forget_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "forget_operation_id", sa.String(36),
            sa.ForeignKey("forget_operations.id"), nullable=False,
        ),
        sa.Column(
            "target_id", sa.String(36),
            sa.ForeignKey("forget_targets.id"), nullable=True,
        ),
        sa.Column(
            "dependency_id", sa.String(36),
            sa.ForeignKey("forget_dependencies.id"), nullable=True,
        ),
        sa.Column("action_type", sa.String(32), nullable=False),
        sa.Column("batch_no", sa.Integer(), server_default=sa.text("0")),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("expected_version", sa.Integer(), nullable=True),
        sa.Column("before_hash", sa.String(64), nullable=True),
        sa.Column("details", json_type, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
    )

    # ── 8. forget_tombstones ──
    op.create_table(
        "forget_tombstones",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "forget_operation_id", sa.String(36),
            sa.ForeignKey("forget_operations.id"), nullable=False,
        ),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("canonical_key", sa.String(256), nullable=True),
        sa.Column("scope_type", sa.String(32), nullable=True),
        sa.Column("scope_id", sa.String(128), nullable=True),
        sa.Column("value_fingerprint", sa.String(64), nullable=True),
        sa.Column("fingerprint_key_version", sa.Integer(), nullable=True),
        sa.Column("block_visibility", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("block_reingestion", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("content_purged", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("allow_audit_read", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("source_event_id", sa.String(36), nullable=True),
        sa.Column("source_turn_record_id", sa.String(36), nullable=True),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_tombstone_op_type_id",
        "forget_tombstones",
        ["forget_operation_id", "target_type", "target_id"],
    )
    # Partial unique: 每条操作内同一 source_event_id 只一条 tombstone
    if is_pg:
        op.execute(sa.text(
            "CREATE UNIQUE INDEX uq_tombstone_op_source_event "
            "ON forget_tombstones (forget_operation_id, source_event_id) "
            "WHERE source_event_id IS NOT NULL"
        ))
        op.execute(sa.text(
            "CREATE UNIQUE INDEX uq_tombstone_op_source_turn "
            "ON forget_tombstones (forget_operation_id, source_turn_record_id) "
            "WHERE source_turn_record_id IS NOT NULL"
        ))
    else:
        # SQLite 部分索引不支持 CREATE UNIQUE INDEX WHERE；降级为普通索引
        op.create_index(
            "ix_tombstone_source_event", "forget_tombstones",
            ["forget_operation_id", "source_event_id"],
            unique=False,
        )
        op.create_index(
            "ix_tombstone_source_turn", "forget_tombstones",
            ["forget_operation_id", "source_turn_record_id"],
            unique=False,
        )
    # 指纹查找索引
    op.create_index(
        "ix_tombstone_fingerprint", "forget_tombstones",
        ["canonical_key", "scope_type", "scope_id", "value_fingerprint"],
    )

    # ── 9. forget_stage_runs ──
    op.create_table(
        "forget_stage_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "forget_operation_id", sa.String(36),
            sa.ForeignKey("forget_operations.id"), nullable=False,
        ),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column(
            "outbox_job_id", sa.String(36),
            sa.ForeignKey("outbox_jobs.id"), nullable=False, unique=True,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("execution_token", sa.String(64), nullable=True),
        sa.Column("claim_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("failure_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    # ── 10. content_provenance_refs ──
    op.create_table(
        "content_provenance_refs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_type", sa.String(32), nullable=False),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_unique_constraint(
        "uq_provenance_owner_source",
        "content_provenance_refs",
        ["owner_type", "owner_id", "source_type", "source_id"],
    )
    op.create_index(
        "ix_provenance_source", "content_provenance_refs",
        ["source_type", "source_id"],
    )


def downgrade() -> None:
    is_pg = op.get_bind().dialect.name == "postgresql"

    # 先删 CHECK 约束
    if is_pg:
        op.drop_constraint("ck_forget_op_status", "forget_operations", type_="check")

    # 先删部分唯一索引
    if is_pg:
        op.execute(sa.text("DROP INDEX IF EXISTS uq_tombstone_op_source_event"))
        op.execute(sa.text("DROP INDEX IF EXISTS uq_tombstone_op_source_turn"))

    # 按依赖逆序删表
    op.drop_table("content_provenance_refs")
    op.drop_table("forget_stage_runs")
    op.drop_table("forget_tombstones")
    op.drop_table("forget_actions")
    op.drop_table("forget_batches")
    op.drop_table("forget_dependencies")
    op.drop_table("forget_targets")
    op.drop_table("forget_shields")
    op.drop_table("forget_selector_manifests")
    op.drop_table("forget_operations")
