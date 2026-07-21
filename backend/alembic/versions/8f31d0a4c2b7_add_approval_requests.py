"""新增工具审批请求表

Revision ID: 8f31d0a4c2b7
Revises: 69da20d1e47c
Create Date: 2026-07-20 19:25:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8f31d0a4c2b7"
down_revision: Union[str, Sequence[str], None] = "69da20d1e47c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建服务端审批唯一事实表。"""
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("turn_record_id", sa.String(length=36), nullable=False),
        sa.Column("turn_id", sa.String(length=36), nullable=False),
        sa.Column("trace_id", sa.String(length=36), nullable=False),
        sa.Column("tool_call_id", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("tool_args", sa.JSON(), nullable=False),
        sa.Column("tool_args_hash", sa.String(length=64), nullable=False),
        sa.Column("descriptor_hash", sa.String(length=64), nullable=False),
        sa.Column("risk_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("execution_token", sa.String(length=36), nullable=True),
        sa.Column("execution_result", sa.JSON(), nullable=True),
        sa.Column("result_event_id", sa.String(length=36), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','executing','succeeded','denied','failed','interrupted_unknown')",
            name="ck_approval_status",
        ),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"]),
        sa.ForeignKeyConstraint(["turn_record_id"], ["turn_records.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("turn_record_id", "tool_call_id", name="uq_approval_turn_tool_call"),
    )
    op.create_index("ix_approval_thread_status", "approval_requests", ["thread_id", "status"], unique=False)
    op.create_index("ix_approval_turn_record", "approval_requests", ["turn_record_id"], unique=False)


def downgrade() -> None:
    """删除服务端审批唯一事实表。"""
    op.drop_index("ix_approval_turn_record", table_name="approval_requests")
    op.drop_index("ix_approval_thread_status", table_name="approval_requests")
    op.drop_table("approval_requests")
