"""Record successful task status reads against the claiming Turn."""

import sqlalchemy as sa
from alembic import op

revision = "a4d7c9e2b681"
down_revision = "e6f90123b405"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("turn_event_inbox", sa.Column("task_status_read_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("turn_event_inbox", "task_status_read_at")
