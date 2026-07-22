"""删除已废弃的旧遗忘请求表

Revision ID: d4f8a1c7e2b9
Revises: c8f5a2d7e914
Create Date: 2026-07-22 17:03:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4f8a1c7e2b9"
down_revision: Union[str, Sequence[str], None] = "c8f5a2d7e914"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """删除不再参与新 Forget Saga 的旧遗忘请求表。"""
    op.drop_index(op.f("ix_forget_requests_memory_id"), table_name="forget_requests")
    op.drop_table("forget_requests")


def downgrade() -> None:
    """重建空的旧兼容表；已删除的历史数据不会恢复。"""
    op.create_table(
        "forget_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("memory_id", sa.String(length=36), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("tombstone", sa.String(length=256), nullable=False),
        sa.Column("saga_state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_forget_requests_memory_id"),
        "forget_requests",
        ["memory_id"],
        unique=False,
    )
