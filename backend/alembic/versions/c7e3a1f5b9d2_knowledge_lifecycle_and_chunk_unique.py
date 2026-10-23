"""知识文档生命周期与分块唯一性

Revision ID: c7e3a1f5b9d2
Revises: b4d7c1e9a2f5
Create Date: 2026-10-23 15:40:00

为知识库维护闭环补充：
- documents 新增 superseded_by / deleted_at / updated_at，status 扩展
  superseded / deleted 两态，并为 source_path 建索引（版本识别与删除定位）。
- chunks 增加 (document_id, chunk_index) 唯一约束，杜绝重复分块；
  迁移前先清理历史重复行（分块是可重建的派生数据）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7e3a1f5b9d2"
down_revision: Union[str, Sequence[str], None] = "b4d7c1e9a2f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DOCUMENT_STATUS_CHECK = (
    "status IN ('indexed','index_failed','source_unavailable','superseded','deleted')"
)
_LEGACY_DOCUMENT_STATUS_CHECK = "status IN ('indexed','index_failed','source_unavailable')"


def upgrade() -> None:
    """扩展文档生命周期状态并约束分块唯一性。"""
    with op.batch_alter_table("documents") as batch_op:
        batch_op.add_column(sa.Column("superseded_by", sa.String(length=36), nullable=True))
        batch_op.add_column(
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.drop_constraint("ck_documents_status", type_="check")
        batch_op.create_check_constraint("ck_documents_status", _DOCUMENT_STATUS_CHECK)
        batch_op.create_index("ix_documents_source_path", ["source_path"], unique=False)

    # 回填后收紧为 NOT NULL，与 ORM 声明（Mapped[datetime]）保持一致
    op.execute("UPDATE documents SET updated_at = created_at WHERE updated_at IS NULL")
    with op.batch_alter_table("documents") as batch_op:
        batch_op.alter_column(
            "updated_at", existing_type=sa.DateTime(timezone=True), nullable=False,
        )

    # 唯一约束前清理历史重复分块：同一 (document_id, chunk_index) 只保留最早一条
    op.execute(
        "DELETE FROM chunks WHERE id NOT IN ("
        "SELECT keep_id FROM ("
        "SELECT MIN(id) AS keep_id FROM chunks GROUP BY document_id, chunk_index"
        ") AS keepers)"
    )
    with op.batch_alter_table("chunks") as batch_op:
        batch_op.create_unique_constraint(
            "uq_chunks_document_chunk_index", ["document_id", "chunk_index"]
        )


def downgrade() -> None:
    """回退分块唯一约束与文档生命周期扩展字段。"""
    with op.batch_alter_table("chunks") as batch_op:
        batch_op.drop_constraint("uq_chunks_document_chunk_index", type_="unique")

    # 新状态在旧约束下非法，回退为可观测的失败态
    op.execute(
        "UPDATE documents SET status='index_failed' "
        "WHERE status IN ('superseded','deleted')"
    )
    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_index("ix_documents_source_path")
        batch_op.drop_constraint("ck_documents_status", type_="check")
        batch_op.create_check_constraint(
            "ck_documents_status", _LEGACY_DOCUMENT_STATUS_CHECK
        )
        batch_op.drop_column("updated_at")
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("superseded_by")
