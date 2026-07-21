"""Phase 4: 长期记忆生命周期维护表与索引。

- 新增表：memory_maintenance_runs / memory_maintenance_batches /
  memory_maintenance_inputs / memory_maintenance_actions
- memory_records 新增列：last_accessed_at（nullable、无默认、不回填）
- 新增 dirty-set 复合索引（对应四条 lane：changed / expired_ephemeral /
  candidate_due / sleep_due）+ neighbor 扩展索引
- 无历史数据回填：last_accessed_at 初始 NULL；不自动改变任何 lifecycle 状态

Revision ID: p4a0b1c2d3e4f
Revises: f3a1b2c4d5e6
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "p4a0b1c2d3e4f"
down_revision: Union[str, Sequence[str], None] = "f3a1b2c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(conn, table: str, column: str) -> bool:
    try:
        insp = sa.inspect(conn)
        cols = [c["name"] for c in insp.get_columns(table)]
        return column in cols
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    json_type = postgresql.JSONB if is_pg else sa.JSON()

    # ── memory_records：新增 last_accessed_at ──
    if not _column_exists(bind, "memory_records", "last_accessed_at"):
        op.add_column(
            "memory_records",
            sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        )

    # ── 新表：memory_maintenance_runs ──
    op.create_table(
        "memory_maintenance_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope_type", sa.String(32), nullable=False, server_default=sa.text("'all_user_memories'")),
        sa.Column("scope_id", sa.String(128), nullable=True),
        sa.Column("outbox_job_id", sa.String(36), sa.ForeignKey("outbox_jobs.id"), nullable=False),
        sa.Column("operation_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'running'")),
        sa.Column("execution_token", sa.String(128), nullable=True),
        sa.Column("claim_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("failure_attempt_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("policy_version", sa.String(32), nullable=False, server_default=sa.text("'phase4.v1'")),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cutoff_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cursor_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor_id", sa.String(36), nullable=True),
        sa.Column("plan_hash", sa.String(64), nullable=True),
        sa.Column("candidate_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("applied_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("skipped_stale_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_maintenance_run_scope_status", "memory_maintenance_runs", ["scope_type", "status"])
    op.create_unique_constraint("uq_maintenance_run_outbox_job", "memory_maintenance_runs", ["outbox_job_id"])
    op.create_unique_constraint("uq_maintenance_run_operation", "memory_maintenance_runs", ["operation_id"])

    # ── 新表：memory_maintenance_batches ──
    op.create_table(
        "memory_maintenance_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("memory_maintenance_runs.id"), nullable=False),
        sa.Column("batch_no", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("candidate_lane", sa.String(32), nullable=False, server_default=sa.text("'changed'")),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'frozen'")),
        sa.Column("lane_cursor_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lane_cursor_id", sa.String(36), nullable=True),
        sa.Column("cursor_start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor_start_id", sa.String(36), nullable=True),
        sa.Column("cursor_end_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor_end_id", sa.String(36), nullable=True),
        sa.Column("input_hash", sa.String(64), nullable=True),
        sa.Column("plan_hash", sa.String(64), nullable=True),
        sa.Column("candidate_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("action_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("applied_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("skipped_stale_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("planned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_maintenance_batch_run_status", "memory_maintenance_batches", ["run_id", "status"])
    op.create_unique_constraint("uq_maintenance_batch_run_no", "memory_maintenance_batches", ["run_id", "batch_no"])

    # ── 新表：memory_maintenance_inputs ──
    op.create_table(
        "memory_maintenance_inputs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("memory_maintenance_runs.id"), nullable=False),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("memory_maintenance_batches.id"), nullable=False),
        sa.Column("input_sequence", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("memory_record_id", sa.String(36), sa.ForeignKey("memory_records.id"), nullable=False),
        sa.Column("input_role", sa.String(16), nullable=False, server_default=sa.text("'seed'")),
        sa.Column("record_version", sa.Integer(), server_default=sa.text("1")),
        sa.Column("record_state_hash", sa.String(64), nullable=True),
        sa.Column("decision_hash", sa.String(64), nullable=True),
        sa.Column("user_required_protected", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("user_required_source_ids", json_type, nullable=True),
        sa.Column("canonical_key", sa.String(256), nullable=True),
        sa.Column("scope_type", sa.String(32), nullable=True),
        sa.Column("scope_id", sa.String(128), nullable=True),
        sa.Column("snapshot_json", json_type, nullable=True),
    )
    op.create_index("ix_maintenance_input_batch", "memory_maintenance_inputs", ["batch_id"])
    op.create_unique_constraint(
        "uq_maintenance_input_batch_record", "memory_maintenance_inputs", ["batch_id", "memory_record_id"]
    )
    op.create_unique_constraint(
        "uq_maintenance_input_batch_seq", "memory_maintenance_inputs", ["batch_id", "input_sequence"]
    )

    # ── 新表：memory_maintenance_actions ──
    op.create_table(
        "memory_maintenance_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("memory_maintenance_runs.id"), nullable=False),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("memory_maintenance_batches.id"), nullable=False),
        sa.Column("action_sequence", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("source_input_ids", json_type, nullable=True),
        sa.Column("operation_group_id", sa.String(64), nullable=True),
        sa.Column("subject_memory_record_id", sa.String(36), sa.ForeignKey("memory_records.id"), nullable=True),
        sa.Column("related_record_ids", json_type, nullable=True),
        sa.Column("canonical_key", sa.String(256), nullable=True),
        sa.Column("scope_type", sa.String(32), nullable=True),
        sa.Column("scope_id", sa.String(128), nullable=True),
        sa.Column("action_type", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("expected_record_version", sa.Integer(), nullable=True),
        sa.Column("preconditions", json_type, nullable=True),
        sa.Column("after_hash", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("details", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_maintenance_action_batch", "memory_maintenance_actions", ["batch_id"])
    op.create_index("ix_maintenance_action_run", "memory_maintenance_actions", ["run_id"])
    op.create_unique_constraint("uq_maintenance_action_idem", "memory_maintenance_actions", ["idempotency_key"])
    op.create_unique_constraint(
        "uq_maintenance_action_batch_seq", "memory_maintenance_actions", ["batch_id", "action_sequence"]
    )

    # ── dirty-set 复合索引（对应四条 lane，第 1 点）──
    op.create_index("ix_memory_records_lifecycle_valid_to", "memory_records", ["lifecycle_state", "valid_to"])
    op.create_index("ix_memory_records_lifecycle_updated", "memory_records", ["lifecycle_state", "updated_at"])
    op.create_index(
        "ix_memory_records_key_scope", "memory_records", ["canonical_key", "scope_type", "scope_id"]
    )
    op.create_index("ix_memory_records_lifecycle_accessed", "memory_records", ["lifecycle_state", "last_accessed_at"])
    op.create_index("ix_memory_records_lifecycle_created", "memory_records", ["lifecycle_state", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_memory_records_lifecycle_created", table_name="memory_records")
    op.drop_index("ix_memory_records_lifecycle_accessed", table_name="memory_records")
    op.drop_index("ix_memory_records_key_scope", table_name="memory_records")
    op.drop_index("ix_memory_records_lifecycle_updated", table_name="memory_records")
    op.drop_index("ix_memory_records_lifecycle_valid_to", table_name="memory_records")

    op.drop_table("memory_maintenance_actions")
    op.drop_table("memory_maintenance_inputs")
    op.drop_table("memory_maintenance_batches")
    op.drop_table("memory_maintenance_runs")

    if _column_exists(bind, "memory_records", "last_accessed_at"):
        op.drop_column("memory_records", "last_accessed_at")
