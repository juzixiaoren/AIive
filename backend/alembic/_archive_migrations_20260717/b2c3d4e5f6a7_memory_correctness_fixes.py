"""Memory correctness fixes: unique index, content_hash, sensitivity, saga_state.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-10 17:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Fix unique index: add validity_state='valid' to WHERE clause
    op.execute("DROP INDEX IF EXISTS ix_memory_records_single_active")
    op.execute("""
        CREATE UNIQUE INDEX uq_memory_records_single_valid_active
        ON memory_records (canonical_key, scope_type, COALESCE(scope_id, ''))
        WHERE lifecycle_state = 'active'
          AND validity_state = 'valid'
          AND cardinality = 'single'
    """)

    # 2. Add content_hash, structured_value_hash, sensitivity
    with op.batch_alter_table("memory_records") as batch_op:
        batch_op.add_column(sa.Column("sensitivity", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("content_hash", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("structured_value_hash", sa.String(32), nullable=True))

    # 3. Add saga_state to forget_requests
    with op.batch_alter_table("forget_requests") as batch_op:
        batch_op.add_column(sa.Column(
            "saga_state", sa.String(32), nullable=False,
            server_default=sa.text("'running'")
        ))

    # 4. Backfill content_hash for existing records
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, content FROM memory_records WHERE content_hash IS NULL"
    )).fetchall()
    for row in rows:
        import hashlib
        ch = hashlib.sha256((row[1] or "").encode()).hexdigest()[:16]
        conn.execute(
            sa.text("UPDATE memory_records SET content_hash = :h WHERE id = :i"),
            {"h": ch, "i": row[0]},
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_memory_records_single_valid_active")
    op.execute("""
        CREATE UNIQUE INDEX ix_memory_records_single_active
        ON memory_records (canonical_key, scope_type, COALESCE(scope_id, ''))
        WHERE lifecycle_state = 'active' AND cardinality = 'single'
    """)
    with op.batch_alter_table("memory_records") as batch_op:
        batch_op.drop_column("structured_value_hash")
        batch_op.drop_column("content_hash")
        batch_op.drop_column("sensitivity")
    with op.batch_alter_table("forget_requests") as batch_op:
        batch_op.drop_column("saga_state")
