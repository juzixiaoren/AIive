"""Phase 5: 冷热历史分层与统一检索（RetrievalIndex）五张新表。

- retrieval_index_generations：全局 generation（唯一 active 作为查询真相源）
- retrieval_index_entries：统一检索投影 Entry（memory_record / segment_summary /
  epoch_checkpoint）
- retrieval_index_tokens：可移植倒排 posting（按 token 精确聚合命中，替代全表 ILIKE）
- retrieval_index_runs：全量 rebuild Run（一 OutboxJob 一 Run）
- retrieval_index_batches：rebuild 有界批次（CONTINUE 分页游标）

无 embedding 列（v1 仅 lexical）。JSON 列在 PostgreSQL 用 JSONB，其余用 JSON。

Revision ID: p5a0b1c2d3e4f
Revises: p4b2c3d4e5f6
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "p5a0b1c2d3e4f"
down_revision: Union[str, Sequence[str], None] = "p4b2c3d4e5f6"
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
    json_type = postgresql.JSONB if is_pg else sa.JSON()

    if not _table_exists(bind, "retrieval_index_generations"):
        op.create_table(
            "retrieval_index_generations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("index_version", sa.BigInteger(), nullable=False, unique=True),
            sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'building'")),
            sa.Column("build_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("build_completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("source_cutoff", sa.DateTime(timezone=True), nullable=True),
            sa.Column("policy_version", sa.String(32), nullable=False, server_default=sa.text("'phase5.v1'")),
            sa.CheckConstraint(
                "status IN ('building','active','retired','failed')",
                name="ck_retrieval_gen_status",
            ),
        )

    if not _table_exists(bind, "retrieval_index_entries"):
        op.create_table(
            "retrieval_index_entries",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("source_id", sa.String(36), nullable=False),
            sa.Column("source_version", sa.String(64), nullable=False),
            sa.Column("source_hash", sa.String(64), nullable=True),
            sa.Column("index_version", sa.BigInteger(), nullable=False),
            sa.Column("thread_id", sa.String(36), nullable=True),
            sa.Column("epoch_id", sa.String(36), nullable=True),
            sa.Column("segment_id", sa.String(36), nullable=True),
            sa.Column("memory_record_id", sa.String(36), nullable=True),
            sa.Column("scope_type", sa.String(32), nullable=False, server_default=sa.text("'global'")),
            sa.Column("scope_id", sa.String(128), nullable=True),
            sa.Column("canonical_key", sa.String(256), nullable=True),
            sa.Column("lifecycle_state", sa.String(32), nullable=True),
            sa.Column("validity_state", sa.String(32), nullable=True),
            sa.Column("retrieval_tier", sa.String(16), nullable=False, server_default=sa.text("'warm'")),
            sa.Column("title", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("search_text", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("snippet", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("metadata", json_type, nullable=True),
            sa.Column("created_source_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_source_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("is_searchable", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.UniqueConstraint(
                "source_type", "source_id", "source_version", "index_version",
                name="uq_retrieval_entry_src_ver_gen",
            ),
            sa.CheckConstraint(
                "source_type IN ('memory_record','segment_summary','epoch_checkpoint')",
                name="ck_retrieval_entry_src",
            ),
            sa.CheckConstraint(
                "retrieval_tier IN ('hot','warm','cold')",
                name="ck_retrieval_entry_tier",
            ),
        )
        op.create_index(
            "ix_retrieval_entry_active", "retrieval_index_entries",
            ["index_version", "is_current", "source_type"],
        )
        op.create_index("ix_retrieval_entry_thread", "retrieval_index_entries", ["thread_id"])
        op.create_index(
            "ix_retrieval_entry_scope", "retrieval_index_entries", ["scope_type", "scope_id"]
        )
        op.create_index("ix_retrieval_entry_tier", "retrieval_index_entries", ["retrieval_tier"])

    if not _table_exists(bind, "retrieval_index_tokens"):
        op.create_table(
            "retrieval_index_tokens",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "entry_id", sa.String(36),
                sa.ForeignKey("retrieval_index_entries.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("index_version", sa.BigInteger(), nullable=False),
            sa.Column("token", sa.String(64), nullable=False),
            sa.Column("token_kind", sa.String(16), nullable=False, server_default=sa.text("'word'")),
            sa.Column("term_frequency", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.UniqueConstraint("entry_id", "token", name="uq_retrieval_token_entry"),
        )
        op.create_index(
            "ix_retrieval_token_lookup", "retrieval_index_tokens",
            ["index_version", "token", "entry_id"],
        )
        op.create_index("ix_retrieval_token_entry", "retrieval_index_tokens", ["entry_id"])

    if not _table_exists(bind, "retrieval_index_runs"):
        op.create_table(
            "retrieval_index_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("outbox_job_id", sa.String(36), sa.ForeignKey("outbox_jobs.id"), nullable=False),
            sa.Column("operation_id", sa.String(128), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'running'")),
            sa.Column("execution_token", sa.String(128), nullable=True),
            sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0")),
            sa.Column("failure_attempt_count", sa.Integer(), server_default=sa.text("0")),
            sa.Column("index_version", sa.BigInteger(), nullable=False),
            sa.Column("source_type", sa.String(32), nullable=True),
            sa.Column("source_id", sa.String(36), nullable=True),
            sa.Column("source_version", sa.String(64), nullable=True),
            sa.Column("batch_cursor", json_type, nullable=True),
            sa.Column("indexed_count", sa.Integer(), server_default=sa.text("0")),
            sa.Column("skipped_stale_count", sa.Integer(), server_default=sa.text("0")),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("outbox_job_id", name="uq_retrieval_run_outbox_job"),
            sa.UniqueConstraint("operation_id", name="uq_retrieval_run_operation"),
        )
        op.create_index("ix_retrieval_run_status", "retrieval_index_runs", ["status"])

    if not _table_exists(bind, "retrieval_index_batches"):
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


def downgrade() -> None:
    bind = op.get_bind()

    if _table_exists(bind, "retrieval_index_batches"):
        op.drop_index("ix_retrieval_batch_run", table_name="retrieval_index_batches")
        op.drop_table("retrieval_index_batches")

    if _table_exists(bind, "retrieval_index_runs"):
        op.drop_index("ix_retrieval_run_status", table_name="retrieval_index_runs")
        op.drop_table("retrieval_index_runs")

    if _table_exists(bind, "retrieval_index_tokens"):
        op.drop_index("ix_retrieval_token_entry", table_name="retrieval_index_tokens")
        op.drop_index("ix_retrieval_token_lookup", table_name="retrieval_index_tokens")
        op.drop_table("retrieval_index_tokens")

    if _table_exists(bind, "retrieval_index_entries"):
        op.drop_index("ix_retrieval_entry_tier", table_name="retrieval_index_entries")
        op.drop_index("ix_retrieval_entry_scope", table_name="retrieval_index_entries")
        op.drop_index("ix_retrieval_entry_thread", table_name="retrieval_index_entries")
        op.drop_index("ix_retrieval_entry_active", table_name="retrieval_index_entries")
        op.drop_table("retrieval_index_entries")

    if _table_exists(bind, "retrieval_index_generations"):
        op.drop_table("retrieval_index_generations")
