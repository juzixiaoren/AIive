"""Phase 6B: 数据保留治理。

新表 (2 张):
  - retention_cleanup_runs  (保留清理运行记录，与 OutboxJob 1:1)
  - retention_cleanup_batches (有界批次，lane + 复合游标)

已有表新列:
  - outbox_jobs.terminal_at
  - retrieval_index_generations.status_changed_at

Revision ID: p6b1a2b3c4d5e6
Revises: p6a1b2c3d4e5
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "p6b1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "1aa51ac0ca11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── retention_cleanup_runs ──
    op.create_table(
        "retention_cleanup_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "outbox_job_id", sa.String(36),
            sa.ForeignKey("outbox_jobs.id"), nullable=False,
        ),
        sa.Column("operation_id", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("policy_snapshot", postgresql.JSON(), nullable=False,
                  server_default=sa.text("'{}'::json")),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("execution_token", sa.String(64), nullable=True),
        sa.Column("claim_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_lane", sa.String(64), nullable=True),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scanned_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scrubbed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_unique_constraint(
        "uq_retention_run_outbox_job", "retention_cleanup_runs", ["outbox_job_id"],
    )
    op.create_unique_constraint(
        "uq_retention_run_operation", "retention_cleanup_runs", ["operation_id"],
    )
    op.create_index(
        "ix_retention_run_status", "retention_cleanup_runs", ["status"],
    )

    # ── retention_cleanup_batches ──
    op.create_table(
        "retention_cleanup_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id", sa.String(36),
            sa.ForeignKey("retention_cleanup_runs.id"), nullable=False,
        ),
        sa.Column("lane", sa.String(64), nullable=False),
        sa.Column("batch_no", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cursor_start_json", postgresql.JSON(), nullable=False,
                  server_default=sa.text("'{}'::json")),
        sa.Column("cursor_end_json", postgresql.JSON(), nullable=True),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("scanned_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scrubbed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_unique_constraint(
        "uq_retention_batch_run_lane_no", "retention_cleanup_batches",
        ["run_id", "lane", "batch_no"],
    )
    op.create_index(
        "ix_retention_batch_run_status", "retention_cleanup_batches",
        ["run_id", "status"],
    )

    # ── outbox_jobs: add terminal_at ──
    op.add_column(
        "outbox_jobs",
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── retrieval_index_generations: add status_changed_at ──
    op.add_column(
        "retrieval_index_generations",
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("retrieval_index_generations", "status_changed_at")
    op.drop_column("outbox_jobs", "terminal_at")
    op.drop_table("retention_cleanup_batches")
    op.drop_table("retention_cleanup_runs")
