"""知识分块结构元数据：标题路径、块类型、来源定位与版本号

Revision ID: b8d2e6f4a1c7
Revises: c3f7b2a9d1e4
Create Date: 2026-11-03

为结构感知切分补齐元数据列，全部使用 server_default，已有数据库无需删除数据：
存量分块的 parser_version / chunker_version 为空字符串，维护任务据此判定「需要
重建」，从持久原文重新解析、切分并刷新向量。

新增列：
* chunks：heading_path、section_id、block_type、source_type、page_start、page_end、
  parser_version、chunker_version、embedding_input_hash
* documents：parser_version、chunker_version（判断是否需要重新解析）
* knowledge_chunk_vectors：input_version（Embedding 输入模板版本）

embedding_input_hash 上建普通索引：维护扫描按「期望哈希 != 投影哈希」找过期投影。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d2e6f4a1c7"
down_revision: str | Sequence[str] | None = "c3f7b2a9d1e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHUNK_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, bool, object], ...] = (
    ("heading_path", sa.String(length=1024), False, ""),
    ("section_id", sa.String(length=64), False, ""),
    ("block_type", sa.String(length=32), False, "text"),
    ("source_type", sa.String(length=32), False, "text"),
    ("page_start", sa.Integer(), True, None),
    ("page_end", sa.Integer(), True, None),
    ("parser_version", sa.String(length=32), False, ""),
    ("chunker_version", sa.String(length=32), False, ""),
    ("embedding_input_hash", sa.String(length=64), False, ""),
)

_DOCUMENT_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, bool, object], ...] = (
    ("parser_version", sa.String(length=32), False, ""),
    ("chunker_version", sa.String(length=32), False, ""),
)


def _add_columns(
    table: str, columns: tuple[tuple[str, sa.types.TypeEngine, bool, object], ...],
) -> None:
    """按 server_default 追加列：已有行自动取默认值，不重建表。"""
    for name, column_type, nullable, default in columns:
        kwargs: dict[str, object] = {"nullable": nullable}
        if default is not None:
            kwargs["server_default"] = default
        op.add_column(table, sa.Column(name, column_type, **kwargs))


def upgrade() -> None:
    """追加结构元数据列（幂等：重复执行由 Alembic 版本表拦截）。"""
    _add_columns("chunks", _CHUNK_COLUMNS)
    op.create_index(
        "ix_chunks_embedding_input_hash", "chunks", ["embedding_input_hash"],
    )
    _add_columns("documents", _DOCUMENT_COLUMNS)
    op.add_column(
        "knowledge_chunk_vectors",
        sa.Column("input_version", sa.String(length=32), nullable=False, server_default=""),
    )


def downgrade() -> None:
    """移除新增列（分块正文与向量仍在，仅丢失结构元数据）。"""
    op.drop_column("knowledge_chunk_vectors", "input_version")
    for name, _type, _nullable, _default in reversed(_DOCUMENT_COLUMNS):
        op.drop_column("documents", name)
    op.drop_index("ix_chunks_embedding_input_hash", table_name="chunks")
    for name, _type, _nullable, _default in reversed(_CHUNK_COLUMNS):
        op.drop_column("chunks", name)
