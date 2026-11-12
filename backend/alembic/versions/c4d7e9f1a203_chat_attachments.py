"""Chat attachments (no binary or extracted text columns)."""

from alembic import op
import sqlalchemy as sa

revision = "c4d7e9f1a203"
down_revision = "b3e6f9c2d7a4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chat_attachments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "thread_id", sa.String(36), sa.ForeignKey("threads.id"), nullable=False
        ),
        sa.Column("turn_record_id", sa.String(36), sa.ForeignKey("turn_records.id")),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("events.id")),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_chat_attachments_thread_id", "chat_attachments", ["thread_id"])
    op.create_index(
        "ix_chat_attachments_turn_record_id", "chat_attachments", ["turn_record_id"]
    )


def downgrade():
    op.drop_table("chat_attachments")
