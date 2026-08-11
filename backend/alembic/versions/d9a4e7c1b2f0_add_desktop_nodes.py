"""新增 Electron Desktop Node 与线程绑定表

Revision ID: d9a4e7c1b2f0
Revises: c6e8f1a2b3d4
Create Date: 2026-08-11 00:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d9a4e7c1b2f0"
down_revision: Union[str, Sequence[str], None] = "c6e8f1a2b3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "desktop_nodes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("arch", sa.String(length=32), nullable=False),
        sa.Column("app_version", sa.String(length=32), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('online','offline')",
            name="ck_desktop_node_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_desktop_nodes_status_lease",
        "desktop_nodes",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_table(
        "thread_desktop_bindings",
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["node_id"], ["desktop_nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("thread_id"),
    )
    op.create_index(
        "ix_thread_desktop_binding_node",
        "thread_desktop_bindings",
        ["node_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_thread_desktop_binding_node", table_name="thread_desktop_bindings")
    op.drop_table("thread_desktop_bindings")
    op.drop_index("ix_desktop_nodes_status_lease", table_name="desktop_nodes")
    op.drop_table("desktop_nodes")
