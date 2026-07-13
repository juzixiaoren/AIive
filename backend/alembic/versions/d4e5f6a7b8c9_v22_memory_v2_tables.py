"""V22: V2 memory read architecture tables.

Adds the three tables introduced by the V2 memory read refactor:
  - core_memory_blocks      (small, stable Core Memory projection)
  - memory_recall_runs      (Automatic / Agent-Initiated recall run trace)
  - memory_recall_candidates(recall candidate trace for the Inspector)

The runtime also creates these via `Base.metadata.create_all()` in
`aiive.main._ensure_schema()`. To keep `alembic upgrade` safe on a database that
was already bootstrapped by `create_all()`, each table is only created when it
does not already exist.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_table_if_missing(op, table_name: str, *args, **kwargs) -> None:
    """Create a table only if it does not already exist.

    Allows this migration to coexist with `create_all()` bootstrapping.
    """
    bind = op.get_bind()
    if bind.dialect.has_table(bind, table_name):
        return
    op.create_table(table_name, *args, **kwargs)


def upgrade() -> None:
    _create_table_if_missing(
        op, "core_memory_blocks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("block_name", sa.String(64), nullable=False, unique=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("source_memory_ids", postgresql.JSONB, server_default=sa.text("'[]'::jsonb")),
        sa.Column("projection_version", sa.Integer, server_default=sa.text("1")),
        sa.Column("record_versions", postgresql.JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("token_count", sa.Integer, server_default=sa.text("0")),
        sa.Column("checksum", sa.String(64), server_default=sa.text("''")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    _create_table_if_missing(
        op, "memory_recall_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("trace_id", sa.String(36), nullable=True),
        sa.Column("request_query", sa.Text, nullable=False),
        sa.Column("scope_context", postgresql.JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("routes_executed", postgresql.JSONB, server_default=sa.text("'[]'::jsonb")),
        sa.Column("token_budget", sa.Integer, server_default=sa.text("0")),
        sa.Column("result_count", sa.Integer, server_default=sa.text("0")),
        sa.Column("total_latency_ms", sa.Float, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    _create_table_if_missing(
        op, "memory_recall_candidates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("memory_id", sa.String(36), nullable=False),
        sa.Column("route", sa.String(32), nullable=False),
        sa.Column("raw_score", sa.Float, server_default=sa.text("0")),
        sa.Column("fused_score", sa.Float, server_default=sa.text("0")),
        sa.Column("selected", sa.Boolean, server_default=sa.text("false")),
        sa.Column("exclusion_reason", sa.String(64), server_default=sa.text("''")),
        sa.Column("token_cost", sa.Integer, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )


def downgrade() -> None:
    op.drop_table("memory_recall_candidates")
    op.drop_table("memory_recall_runs")
    op.drop_table("core_memory_blocks")
