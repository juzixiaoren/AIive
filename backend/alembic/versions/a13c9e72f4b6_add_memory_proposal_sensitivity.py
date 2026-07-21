"""为记忆提案增加敏感度审计字段并回填历史记录

Revision ID: a13c9e72f4b6
Revises: e7b29c4d8a11
Create Date: 2026-07-20 20:40:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a13c9e72f4b6"
down_revision: Union[str, Sequence[str], None] = "e7b29c4d8a11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """增加提案敏感度列，并将历史记忆 NULL 明确回填为 normal。"""
    op.add_column(
        "memory_proposals",
        sa.Column("sensitivity", sa.String(length=32), nullable=True),
    )
    op.execute("UPDATE memory_records SET sensitivity = 'normal' WHERE sensitivity IS NULL")
    op.execute("UPDATE memory_proposals SET sensitivity = 'normal' WHERE sensitivity IS NULL")


def downgrade() -> None:
    """移除提案敏感度列；memory_records 的 normal 回填保持不变。"""
    op.drop_column("memory_proposals", "sensitivity")
