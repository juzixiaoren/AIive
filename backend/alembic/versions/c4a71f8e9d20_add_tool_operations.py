"""新增持久化副作用工具操作表

Revision ID: c4a71f8e9d20
Revises: 8f31d0a4c2b7
Create Date: 2026-07-20 19:45:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4a71f8e9d20"
down_revision: Union[str, Sequence[str], None] = "8f31d0a4c2b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建副作用工具操作唯一事实表。"""
    op.create_table(
        "tool_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("turn_record_id", sa.String(length=36), nullable=False),
        sa.Column("turn_id", sa.String(length=36), nullable=False),
        sa.Column("tool_call_id", sa.String(length=128), nullable=False),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("params_hash", sa.String(length=64), nullable=False),
        sa.Column("descriptor_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("effect_mode", sa.String(length=32), nullable=False),
        sa.Column("outbox_job_id", sa.String(length=36), nullable=False),
        sa.Column("execution_token", sa.String(length=36), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=True),
        sa.Column("effect_receipt", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("terminal_reason", sa.String(length=64), nullable=True),
        sa.Column("trace_id", sa.String(length=36), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued','running','committed','failed','execution_unknown')",
            name="ck_tool_operation_status",
        ),
        sa.CheckConstraint(
            "effect_mode IN ('db_transactional','externally_reconcilable','non_repeatable_external')",
            name="ck_tool_operation_effect_mode",
        ),
        sa.ForeignKeyConstraint(["outbox_job_id"], ["outbox_jobs.id"]),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"]),
        sa.ForeignKeyConstraint(["turn_record_id"], ["turn_records.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("outbox_job_id"),
        sa.UniqueConstraint("turn_record_id", "tool_call_id", name="uq_tool_operation_turn_call"),
    )
    op.create_index("ix_tool_operation_thread_status", "tool_operations", ["thread_id", "status"], unique=False)
    op.create_index("ix_tool_operations_trace_id", "tool_operations", ["trace_id"], unique=False)


def downgrade() -> None:
    """删除副作用工具操作表。"""
    op.drop_index("ix_tool_operations_trace_id", table_name="tool_operations")
    op.drop_index("ix_tool_operation_thread_status", table_name="tool_operations")
    op.drop_table("tool_operations")
