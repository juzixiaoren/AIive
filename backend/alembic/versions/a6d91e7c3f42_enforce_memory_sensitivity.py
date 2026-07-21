"""收紧记忆敏感度字段约束

Revision ID: a6d91e7c3f42
Revises: f2c8a91d4e60
Create Date: 2026-07-21 10:45:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a6d91e7c3f42"
down_revision: Union[str, Sequence[str], None] = "f2c8a91d4e60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ALLOWED = "('normal','personal','confidential','secret')"


def upgrade() -> None:
    """回填旧空值，并限制敏感度为非空领域枚举。"""
    op.execute("UPDATE memory_records SET sensitivity = 'normal' WHERE sensitivity IS NULL")
    with op.batch_alter_table("memory_records") as batch_op:
        batch_op.alter_column(
            "sensitivity",
            existing_type=sa.String(length=32),
            nullable=False,
            server_default="normal",
        )
        batch_op.create_check_constraint(
            "ck_memory_records_sensitivity",
            f"sensitivity IN {_ALLOWED}",
        )


def downgrade() -> None:
    """恢复可空敏感度字段并移除值域约束。"""
    with op.batch_alter_table("memory_records") as batch_op:
        batch_op.drop_constraint("ck_memory_records_sensitivity", type_="check")
        batch_op.alter_column(
            "sensitivity",
            existing_type=sa.String(length=32),
            nullable=True,
            server_default=None,
        )
