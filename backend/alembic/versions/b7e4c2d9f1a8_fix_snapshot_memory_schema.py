"""修复上下文与记忆数据完整性

Revision ID: b7e4c2d9f1a8
Revises: a6d91e7c3f42
Create Date: 2026-07-21 14:05:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7e4c2d9f1a8"
down_revision: Union[str, Sequence[str], None] = "a6d91e7c3f42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """删除冗余谱系字段，并补齐召回候选的运行外键。"""
    with op.batch_alter_table("memory_records") as batch_op:
        batch_op.drop_column("lineage")
    op.execute(
        "DELETE FROM memory_recall_candidates "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM memory_recall_runs "
        "WHERE memory_recall_runs.id = memory_recall_candidates.run_id"
        ")"
    )
    with op.batch_alter_table("memory_recall_candidates") as batch_op:
        batch_op.create_foreign_key(
            "fk_memory_recall_candidates_run_id",
            "memory_recall_runs",
            ["run_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    """恢复冗余谱系字段，并移除召回候选运行外键。"""
    with op.batch_alter_table("memory_recall_candidates") as batch_op:
        batch_op.drop_constraint("fk_memory_recall_candidates_run_id", type_="foreignkey")
    with op.batch_alter_table("memory_records") as batch_op:
        batch_op.add_column(sa.Column("lineage", sa.String(length=128), nullable=True))
