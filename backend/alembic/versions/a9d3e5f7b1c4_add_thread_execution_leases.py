"""新增 thread_execution_leases：前台 Turn 与后台目标决策的线程级互斥占用

只保存"谁有权在该线程调用 LLM 并写入时间线"的租约事实：owner_kind/owner_id
标识持有者，fencing_token 单调递增拒绝过期写入，lease_expires_at 让进程崩溃
后的占用自然失效并可被接管。抢占是条件更新，不跨 LLM 调用持有事务。

Revision ID: a9d3e5f7b1c4
Revises: f7a1c3d5e9b2
Create Date: 2026-10-28 15:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a9d3e5f7b1c4"
down_revision: Union[str, Sequence[str], None] = "f7a1c3d5e9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "thread_execution_leases",
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("owner_kind", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("thread_id"),
    )
    op.create_index(
        "ix_thread_execution_leases_expiry",
        "thread_execution_leases",
        ["lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_thread_execution_leases_expiry", table_name="thread_execution_leases"
    )
    op.drop_table("thread_execution_leases")
