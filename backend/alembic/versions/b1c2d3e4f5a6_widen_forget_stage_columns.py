"""放宽 forget stage 相关列长度

forget_stage_runs.stage 与 forget_batches.stage 使用 FORGET_STAGE_ORDER 枚举值，
其中 'rebuild_dependencies' 长 19 字符，原 String(16) 在 PostgreSQL 下插入即报
StringDataRightTruncation。统一放宽到 String(32)。存量数据均远短于此，无需回填。

Revision ID: b1c2d3e4f5a6
Revises: a9b2c4d6e8f0
Create Date: 2026-07-25 16:20:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "a9b2c4d6e8f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """stage 列放宽到 VARCHAR(32)。"""
    with op.batch_alter_table("forget_stage_runs") as batch_op:
        batch_op.alter_column(
            "stage",
            type_=sa.String(length=32),
            existing_type=sa.String(length=16),
            existing_nullable=False,
        )
    with op.batch_alter_table("forget_batches") as batch_op:
        batch_op.alter_column(
            "stage",
            type_=sa.String(length=32),
            existing_type=sa.String(length=16),
            existing_nullable=False,
        )


def downgrade() -> None:
    """stage 列缩回 VARCHAR(16)（仅当无 rebuild_dependencies 等超长值时安全）。"""
    with op.batch_alter_table("forget_batches") as batch_op:
        batch_op.alter_column(
            "stage",
            type_=sa.String(length=16),
            existing_type=sa.String(length=32),
            existing_nullable=False,
        )
    with op.batch_alter_table("forget_stage_runs") as batch_op:
        batch_op.alter_column(
            "stage",
            type_=sa.String(length=16),
            existing_type=sa.String(length=32),
            existing_nullable=False,
        )
