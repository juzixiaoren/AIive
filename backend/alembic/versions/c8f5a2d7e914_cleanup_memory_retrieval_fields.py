"""清理记忆兼容字段并修正检索候选来源语义

Revision ID: c8f5a2d7e914
Revises: b7e4c2d9f1a8
Create Date: 2026-07-21 16:35:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8f5a2d7e914"
down_revision: Union[str, Sequence[str], None] = "b7e4c2d9f1a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """删除无消费者的记忆字段，并把检索候选改为多态来源标识。"""
    with op.batch_alter_table("memory_records") as batch_op:
        batch_op.drop_index("ix_memory_records_memory_key")
        batch_op.drop_column("memory_key")
        batch_op.drop_column("merged_from")

    with op.batch_alter_table("retrieval_candidates") as batch_op:
        batch_op.alter_column(
            "chunk_id",
            new_column_name="source_id",
            existing_type=sa.String(length=36),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "source",
            new_column_name="source_type",
            existing_type=sa.String(length=32),
            existing_nullable=False,
        )


def downgrade() -> None:
    """恢复旧列结构；已删除的兼容字段数据不会恢复。"""
    with op.batch_alter_table("retrieval_candidates") as batch_op:
        batch_op.alter_column(
            "source_id",
            new_column_name="chunk_id",
            existing_type=sa.String(length=36),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "source_type",
            new_column_name="source",
            existing_type=sa.String(length=32),
            existing_nullable=False,
        )

    with op.batch_alter_table("memory_records") as batch_op:
        batch_op.add_column(sa.Column("memory_key", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("merged_from", sa.JSON(), nullable=True))
        batch_op.create_index("ix_memory_records_memory_key", ["memory_key"], unique=False)
