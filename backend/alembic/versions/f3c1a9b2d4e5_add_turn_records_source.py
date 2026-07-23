"""为 turn_records 增加结构化来源列 source

取代 turn_id 前缀命名约定（system_ / runtime_），作为前端展示与上下文
注入的统一判定依据。存量行由 server_default 统一回填为 user。

Revision ID: f3c1a9b2d4e5
Revises: d4f8a1c7e2b9
Create Date: 2026-07-23 16:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3c1a9b2d4e5"
down_revision: Union[str, Sequence[str], None] = "d4f8a1c7e2b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增 source 列；存量行由 server_default 统一回填为 user。"""
    op.add_column(
        "turn_records",
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default="user",
        ),
    )


def downgrade() -> None:
    """移除 source 列，回退到 turn_id 前缀约定。"""
    op.drop_column("turn_records", "source")
