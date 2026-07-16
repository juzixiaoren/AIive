"""Phase 5 检索运维变更：generation.backfill_done + 删除 RetrievalIndexRun 冗余列。

- retrieval_index_generations 增加 backfill_done（bootstrap 是否已完成初始全量回填）
- 删除 retrieval_index_runs 的冗余列 source_type / source_id / source_version
  （rebuild 仅使用 batch_cursor，这三列从未被读写）

Revision ID: p5c1d2e3f4a5b
Revises: p5b3c4d5e6f7
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "p5c1d2e3f4a5b"
down_revision: Union[str, Sequence[str], None] = "p5b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # ── 新增 generation.backfill_done ──
    if is_pg:
        op.add_column(
            "retrieval_index_generations",
            sa.Column("backfill_done", sa.Boolean(), server_default=sa.false(), nullable=False),
        )
    else:
        op.add_column(
            "retrieval_index_generations",
            sa.Column("backfill_done", sa.Boolean(), server_default=sa.false(), nullable=False),
        )

    # ── 删除 RetrievalIndexRun 冗余列 ──
    with op.batch_alter_table("retrieval_index_runs") as batch:
        batch.drop_column("source_type")
        batch.drop_column("source_id")
        batch.drop_column("source_version")


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    with op.batch_alter_table("retrieval_index_runs") as batch:
        batch.add_column(
            sa.Column("source_type", sa.String(32), nullable=True),
        )
        batch.add_column(
            sa.Column("source_id", sa.String(36), nullable=True),
        )
        batch.add_column(
            sa.Column("source_version", sa.String(64), nullable=True),
        )

    if is_pg:
        op.drop_column("retrieval_index_generations", "backfill_done")
    else:
        op.drop_column("retrieval_index_generations", "backfill_done")
