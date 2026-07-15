"""Phase 0.5A: TurnRecord + Event turn tracking columns.

- TurnRecord table with idempotency, lease, heartbeat support
- Event.turn_id + Event.turn_event_index columns
- PostgreSQL SEQUENCE for turn_sequence
- Partial unique index for turn event ordering

Revision ID: b5e6f7a8c9d0
Revises: d4e5f6a7b8c9
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'b5e6f7a8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_table_if_missing(op, table_name: str, *args, **kwargs) -> None:
    bind = op.get_bind()
    if bind.dialect.has_table(bind, table_name):
        return
    op.create_table(table_name, *args, **kwargs)


def upgrade() -> None:
    # ── TurnRecord ──
    _create_table_if_missing(
        op, "turn_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("thread_id", sa.String(36), sa.ForeignKey("threads.id"), nullable=False),
        sa.Column("turn_id", sa.String(36), nullable=False),
        sa.Column("turn_sequence", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'not_started'")),
        sa.Column("execution_id", sa.String(36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_no", sa.Integer(), server_default=sa.text("1")),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_event_id", sa.String(36), nullable=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=False, server_default=sa.text("''")),
        sa.Column("response_payload", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_unique_constraint("uq_turn_record_thread_turn", "turn_records", ["thread_id", "turn_id"])
    op.create_index("ix_turn_records_thread_status", "turn_records", ["thread_id", "status"])
    op.create_index("ix_turn_records_thread_sequence", "turn_records", ["thread_id", "turn_sequence"])

    # PostgreSQL SEQUENCE for turn_sequence
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("CREATE SEQUENCE IF NOT EXISTS turn_sequence_seq START 1 INCREMENT 1"))
        op.execute(sa.text(
            "ALTER TABLE turn_records ALTER COLUMN turn_sequence SET DEFAULT nextval('turn_sequence_seq')"
        ))

    # ── Event new columns ──
    op.add_column("events", sa.Column("turn_id", sa.String(36), nullable=True))
    op.add_column("events", sa.Column("turn_event_index", sa.Integer(), nullable=True))

    # Partial unique index (PostgreSQL only)
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(
            "CREATE UNIQUE INDEX ix_events_turn_order_unique "
            "ON events (thread_id, turn_id, turn_event_index) "
            "WHERE turn_id IS NOT NULL AND turn_event_index IS NOT NULL"
        ))
    op.create_index("ix_events_turn_order", "events", ["thread_id", "turn_id", "turn_event_index"])


def downgrade() -> None:
    op.drop_index("ix_events_turn_order", table_name="events")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP INDEX IF EXISTS ix_events_turn_order_unique"))
    op.drop_column("events", "turn_event_index")
    op.drop_column("events", "turn_id")

    op.drop_index("ix_turn_records_thread_sequence", table_name="turn_records")
    op.drop_index("ix_turn_records_thread_status", table_name="turn_records")
    op.drop_constraint("uq_turn_record_thread_turn", "turn_records")
    op.drop_table("turn_records")

    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP SEQUENCE IF EXISTS turn_sequence_seq"))
