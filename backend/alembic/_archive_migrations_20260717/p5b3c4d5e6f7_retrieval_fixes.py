"""Phase 5 检索索引修复：partial unique index + drop 死表 + data migration。

- 清理可能存在的重复 current 行（同 source/generation 保留最新版本）
- 新增部分唯一索引：uq_retrieval_entry_current
- 删除未使用的 retrieval_index_batches 表

Revision ID: p5b3c4d5e6f7
Revises: p5a0b1c2d3e4f
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "p5b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "p5a0b1c2d3e4f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(conn, table: str) -> bool:
    try:
        return conn.dialect.has_table(conn, table)
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # ── Data migration: 清理重复 current 行 ──
    # 同 (index_version, source_type, source_id) 内可能有多条 is_current=TRUE 的历史遗留。
    # 按 source_version 数值降序，仅保留最新一条 current，其余置 False。
    if is_pg:
        op.execute(sa.text("""
            UPDATE retrieval_index_entries AS r
            SET is_current = FALSE
            WHERE r.is_current = TRUE
              AND r.id NOT IN (
                  SELECT DISTINCT ON (index_version, source_type, source_id) id
                  FROM retrieval_index_entries
                  WHERE is_current = TRUE
                  ORDER BY index_version, source_type, source_id, CAST(source_version AS INTEGER) DESC
              )
        """))
    else:
        # SQLite: 用子查询分组保留每组的最高 source_version
        op.execute(sa.text("""
            UPDATE retrieval_index_entries
            SET is_current = 0
            WHERE is_current = 1
              AND id NOT IN (
                  SELECT id FROM (
                      SELECT id, source_type, source_id, index_version,
                             CAST(source_version AS INTEGER) AS sv_int
                      FROM retrieval_index_entries
                      WHERE is_current = 1
                      GROUP BY index_version, source_type, source_id
                      HAVING CAST(source_version AS INTEGER) = MAX(CAST(source_version AS INTEGER))
                  )
              )
        """))

    # ── 新增部分唯一索引 ──
    if _table_exists(bind, "retrieval_index_entries"):
        # 先检查索引是否存在（幂等）
        try:
            if is_pg:
                op.execute(sa.text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_retrieval_entry_current "
                    "ON retrieval_index_entries (index_version, source_type, source_id) "
                    "WHERE is_current IS TRUE"
                ))
            else:
                op.execute(sa.text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_retrieval_entry_current "
                    "ON retrieval_index_entries (index_version, source_type, source_id) "
                    "WHERE is_current = 1"
                ))
        except Exception:
            # 索引已存在或 SQLite 版本不支持 partial unique → 跳过
            pass

    # ── 删除未使用的 retrieval_index_batches 表 ──
    if _table_exists(bind, "retrieval_index_batches"):
        if is_pg:
            op.execute(sa.text("DROP TABLE IF EXISTS retrieval_index_batches CASCADE"))
        else:
            op.execute(sa.text("DROP TABLE IF EXISTS retrieval_index_batches"))


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # 移除 partial unique index
    try:
        if is_pg:
            op.execute(sa.text("DROP INDEX IF EXISTS uq_retrieval_entry_current"))
        else:
            op.execute(sa.text("DROP INDEX IF EXISTS uq_retrieval_entry_current"))
    except Exception:
        pass

    # 重建 retrieval_index_batches 表
    if not _table_exists(bind, "retrieval_index_batches"):
        json_type = sa.dialects.postgresql.JSONB if is_pg else sa.JSON()
        op.create_table(
            "retrieval_index_batches",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("retrieval_index_runs.id"), nullable=False),
            sa.Column("batch_no", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
            sa.Column("source_type", sa.String(32), nullable=False, server_default=sa.text("'memory_record'")),
            sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'frozen'")),
            sa.Column("cursor", json_type, nullable=True),
            sa.Column("input_hash", sa.String(64), nullable=True),
            sa.Column("indexed_count", sa.Integer(), server_default=sa.text("0")),
            sa.Column("skipped_stale_count", sa.Integer(), server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("run_id", "batch_no", name="uq_retrieval_batch_run_no"),
        )
        op.create_index("ix_retrieval_batch_run", "retrieval_index_batches", ["run_id"])
