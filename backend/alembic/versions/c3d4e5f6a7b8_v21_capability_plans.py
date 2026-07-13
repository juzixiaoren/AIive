"""V21: capability_plans table for MCP self-bootstrap.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "capability_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("goal", sa.Text, nullable=False),
        sa.Column("goal_summary", sa.String(256), nullable=True),
        sa.Column("missing_capability_type", sa.String(64), nullable=True),
        sa.Column("candidates", postgresql.JSONB, nullable=True),
        sa.Column("risk_scores", postgresql.JSONB, nullable=True),
        sa.Column("selected_candidate", sa.String(256), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'proposed'"), index=True),
        sa.Column("install_plan", postgresql.JSONB, nullable=True),
        sa.Column("smoke_result", postgresql.JSONB, nullable=True),
        sa.Column("capability_id", sa.String(128), nullable=True),
        sa.Column("lesson_memory_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )


def downgrade() -> None:
    op.drop_table("capability_plans")
