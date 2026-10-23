"""能力版本记录扩展生命周期字段

Revision ID: b4d7c1e9a2f5
Revises: e2f6a8c4d1b7
Create Date: 2026-10-23 10:20:00

回滚要还原"定义 + 注册 + 状态"，仅记录版本号与指纹不够：
新增 source / state / sandbox_path / definition_snapshot 四列（全部可空，
既有行无需回填）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b4d7c1e9a2f5"
down_revision: Union[str, Sequence[str], None] = "e2f6a8c4d1b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "capability_versions",
        sa.Column("source", sa.String(64), nullable=True),
    )
    op.add_column(
        "capability_versions",
        sa.Column("state", sa.String(32), nullable=True),
    )
    op.add_column(
        "capability_versions",
        sa.Column("sandbox_path", sa.String(512), nullable=True),
    )
    op.add_column(
        "capability_versions",
        sa.Column("definition_snapshot", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("capability_versions", "definition_snapshot")
    op.drop_column("capability_versions", "sandbox_path")
    op.drop_column("capability_versions", "state")
    op.drop_column("capability_versions", "source")
