"""Phase 2: memory_records.retention_policy, memory_proposals.execution_mode/retention_policy/valid_to/durable/source_turn_record_id

Revision ID: 0205b4c1d
Revises: ec517a918616
Create Date: 2026-07-15 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0205b4c1d"
down_revision: Union[str, None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── memory_records ──
    op.add_column(
        "memory_records",
        sa.Column("retention_policy", sa.String(32), nullable=False, server_default="normal"),
    )

    # ── memory_proposals ──
    op.add_column(
        "memory_proposals",
        sa.Column("execution_mode", sa.String(32), nullable=False, server_default="system_best_effort"),
    )
    op.add_column(
        "memory_proposals",
        sa.Column("retention_policy", sa.String(32), nullable=False, server_default="normal"),
    )
    op.add_column(
        "memory_proposals",
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "memory_proposals",
        sa.Column("durable", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "memory_proposals",
        sa.Column("source_turn_record_id", sa.String(36), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("memory_proposals", "source_turn_record_id")
    op.drop_column("memory_proposals", "durable")
    op.drop_column("memory_proposals", "valid_to")
    op.drop_column("memory_proposals", "retention_policy")
    op.drop_column("memory_proposals", "execution_mode")
    op.drop_column("memory_records", "retention_policy")
