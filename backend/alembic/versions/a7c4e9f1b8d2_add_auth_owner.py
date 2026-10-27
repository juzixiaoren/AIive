"""新增单用户所有者账号表

Revision ID: a7c4e9f1b8d2
Revises: d5c1a7b9e4f2
Create Date: 2026-10-26 12:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a7c4e9f1b8d2"
down_revision: Union[str, Sequence[str], None] = "d5c1a7b9e4f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "auth_owners",
        sa.Column("id", sa.String(36), nullable=False),
        # singleton 唯一约束是"只允许一个所有者账号"的数据库级保证。
        sa.Column("singleton", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("singleton", name="uq_auth_owners_singleton"),
        sa.UniqueConstraint("username", name="uq_auth_owners_username"),
    )


def downgrade() -> None:
    op.drop_table("auth_owners")
