"""Bound attachment history lookup by source Event."""

from alembic import op

revision = "d5e8f102b304"
down_revision = "c4d7e9f1a203"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_chat_attachments_event_id", "chat_attachments", ["event_id"])


def downgrade():
    op.drop_index("ix_chat_attachments_event_id", "chat_attachments")
