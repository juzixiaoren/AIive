"""新增记忆 pgvector 投影

Revision ID: e7b29c4d8a11
Revises: c4a71f8e9d20
Create Date: 2026-07-20 20:10:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


revision: str = "e7b29c4d8a11"
down_revision: Union[str, Sequence[str], None] = "c4a71f8e9d20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """启用 vector 扩展并创建固定 1536 维的记忆向量投影表。"""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "memory_vector_projections",
        sa.Column("memory_id", sa.String(length=36), nullable=False),
        sa.Column("record_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=32), nullable=True),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["memory_id"], ["memory_records.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("memory_id"),
    )
    op.create_index(
        "ix_memory_vector_embedding_hnsw",
        "memory_vector_projections",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    """删除记忆向量投影表；vector 扩展可能被其他表使用，因此不移除扩展。"""
    op.drop_index("ix_memory_vector_embedding_hnsw", table_name="memory_vector_projections")
    op.drop_table("memory_vector_projections")
