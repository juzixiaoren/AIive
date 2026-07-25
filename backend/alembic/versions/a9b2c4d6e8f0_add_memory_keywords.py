"""为 memory_records 增加关键词列 keywords

允许 agent 在 remember_or_update 时显式附加检索关键词，使词汇召回（lexical /
MemorySearch）不仅匹配 content/canonical_key，也能命中同义词/上位词（例如记下
「我喜欢霸王茶姬」时附关键词「奶茶」，查询「想喝奶茶」即可召回）。

存量行无关键词，列可空，无需回填。

Revision ID: a9b2c4d6e8f0
Revises: f3c1a9b2d4e5
Create Date: 2026-07-25 16:12:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a9b2c4d6e8f0"
down_revision: Union[str, Sequence[str], None] = "f3c1a9b2d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增 keywords JSON 列（可空），供词汇召回匹配。"""
    op.add_column(
        "memory_records",
        sa.Column("keywords", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    """移除 keywords 列。"""
    op.drop_column("memory_records", "keywords")
