"""Persist instance-wide tool trust settings."""

import sqlalchemy as sa
from alembic import op

revision = "b6f801d924ac"
down_revision = "a4d7c9e2b681"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = op.create_table(
        "tool_trust_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("approval_exempt_capabilities", sa.JSON(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_tool_trust_settings_singleton"),
    )
    op.bulk_insert(table, [{"id": 1, "mode": "standard", "approval_exempt_capabilities": []}])


def downgrade() -> None:
    op.drop_table("tool_trust_settings")
