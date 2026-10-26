"""新增知识分块 pgvector 投影（派生数据）

Revision ID: d5c1a7b9e4f2
Revises: b7f3c1d9a4e2
Create Date: 2026-10-26 11:30:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "d5c1a7b9e4f2"
down_revision: str | Sequence[str] | None = "b7f3c1d9a4e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建知识分块向量投影表与 HNSW 近邻索引。

    表内容是 `chunks` 的派生投影：不新增事实字段，可随时 TRUNCATE 后由
    `knowledge.maintenance` 回填重建。
    """
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "knowledge_chunk_vectors",
        sa.Column("chunk_id", sa.String(length=36), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("embedding", Vector(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chunk_id"),
    )
    op.create_index(
        "ix_knowledge_chunk_vector_embedding_hnsw",
        "knowledge_chunk_vectors",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    """删除知识分块向量投影表；vector 扩展可能被其他表使用，因此不移除扩展。"""
    op.drop_index(
        "ix_knowledge_chunk_vector_embedding_hnsw",
        table_name="knowledge_chunk_vectors",
    )
    op.drop_table("knowledge_chunk_vectors")
