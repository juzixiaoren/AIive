"""将记忆向量投影调整为 BGE-small 的 512 维

memory_vector_projections 是可重建的派生投影。不同 embedding 模型产生的向量
不能互相转换或比较，因此迁移时清空旧 1536 维投影，再由启动 bootstrap 依据
MemoryRecord 真相源异步重建 512 维投影。

Revision ID: c6e8f1a2b3d4
Revises: b1c2d3e4f5a6
Create Date: 2026-07-29 00:00:00
"""
from typing import Sequence, Union

from alembic import op


revision: str = "c6e8f1a2b3d4"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rebuild_postgres_projection_dimension(target: int) -> None:
    op.drop_index(
        "ix_memory_vector_embedding_hnsw",
        table_name="memory_vector_projections",
    )
    op.execute("TRUNCATE TABLE memory_vector_projections")
    op.execute(
        "ALTER TABLE memory_vector_projections "
        f"ALTER COLUMN embedding TYPE vector({target})"
    )
    op.create_index(
        "ix_memory_vector_embedding_hnsw",
        "memory_vector_projections",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def upgrade() -> None:
    """清空可重建投影并将 PostgreSQL vector(1536) 改为 vector(512)。"""
    if op.get_bind().dialect.name == "postgresql":
        _rebuild_postgres_projection_dimension(512)


def downgrade() -> None:
    """清空可重建投影并恢复 PostgreSQL vector(1536)。"""
    if op.get_bind().dialect.name == "postgresql":
        _rebuild_postgres_projection_dimension(1536)
