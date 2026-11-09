"""单用户唯一长期对话：conversation_state 单例 + Turn 取消信号

- conversation_state 保存唯一 current_thread_id（后端是 Thread ID 唯一事实源）；
- turn_records 增加 cancel_requested_at，作为跨进程/跨请求的持久化取消信号。

Revision ID: b3e6f9c2d7a4
Revises: f1a2c3d4e5b6
Create Date: 2026-11-09 10:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b3e6f9c2d7a4"
down_revision: Union[str, Sequence[str], None] = "f1a2c3d4e5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增取消信号列与当前对话单例表。"""
    op.add_column(
        "turn_records",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "conversation_state",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("current_thread_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["current_thread_id"], ["threads.id"], ondelete="RESTRICT",
        ),
    )


def downgrade() -> None:
    """回退取消信号列与单例表。"""
    op.drop_table("conversation_state")
    op.drop_column("turn_records", "cancel_requested_at")
