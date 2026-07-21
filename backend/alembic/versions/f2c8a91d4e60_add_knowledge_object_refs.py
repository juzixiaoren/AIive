"""新增知识原文对象引用与索引状态

Revision ID: f2c8a91d4e60
Revises: a13c9e72f4b6
Create Date: 2026-07-20 20:35:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f2c8a91d4e60"
down_revision: Union[str, Sequence[str], None] = "a13c9e72f4b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """为知识文档添加持久对象引用和可审计索引状态。"""
    with op.batch_alter_table("documents") as batch_op:
        batch_op.add_column(sa.Column("object_bucket", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("object_key", sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column("content_size", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "mime_type", sa.String(length=255), nullable=False,
                server_default="application/octet-stream",
            )
        )
        batch_op.add_column(
            sa.Column(
                "status", sa.String(length=32), nullable=False,
                server_default="source_unavailable",
            )
        )
        batch_op.create_check_constraint(
            "ck_documents_status",
            "status IN ('indexed','index_failed','source_unavailable')",
        )
        batch_op.create_index("ix_documents_status", ["status"], unique=False)


def downgrade() -> None:
    """移除知识原文对象引用和索引状态字段。"""
    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_index("ix_documents_status")
        batch_op.drop_constraint("ck_documents_status", type_="check")
        batch_op.drop_column("status")
        batch_op.drop_column("mime_type")
        batch_op.drop_column("content_size")
        batch_op.drop_column("object_key")
        batch_op.drop_column("object_bucket")
