"""新增 turn_event_inbox：Turn 执行期间到达事件的持久化收件箱

事件仍由 Outbox 可靠投递；本表只保存等待在安全边界注入当前 Turn 的事件视图。
dedupe_key 唯一约束保证重复发布只落一行，status 支持重启后由 Outbox 兜底处理。

Revision ID: f7a1c3d5e9b2
Revises: a7c4e9f1b8d2
Create Date: 2026-10-28 10:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f7a1c3d5e9b2"
down_revision: Union[str, Sequence[str], None] = "a7c4e9f1b8d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "turn_event_inbox",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("turn_record_id", sa.String(length=36), nullable=True),
        sa.Column("turn_id", sa.String(length=36), nullable=True),
        sa.Column("decision", sa.String(length=32), nullable=True),
        sa.Column("message_event_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("injected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index(
        "ix_turn_event_inbox_thread_id", "turn_event_inbox", ["thread_id"], unique=False
    )
    op.create_index(
        "ix_turn_event_inbox_status", "turn_event_inbox", ["status"], unique=False
    )
    op.create_index(
        "ix_turn_event_inbox_thread_status",
        "turn_event_inbox",
        ["thread_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_turn_event_inbox_thread_status", table_name="turn_event_inbox")
    op.drop_index("ix_turn_event_inbox_status", table_name="turn_event_inbox")
    op.drop_index("ix_turn_event_inbox_thread_id", table_name="turn_event_inbox")
    op.drop_table("turn_event_inbox")
