"""Persist Experience V1 episodes, observations and insight versions."""
import sqlalchemy as sa
from alembic import op

revision = "6d9e20b18c4a"
down_revision = "b6f801d924ac"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "experience_episodes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("parent_id", sa.String(36)),
        sa.Column("task_type", sa.String(64), nullable=False),
        sa.Column("family", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("refs", sa.JSON(), nullable=False),
        sa.Column("uses", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reflected_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("kind", "source_id", name="uq_experience_episode_source"),
    )
    for field in ("thread_id", "parent_id", "completed_at", "reflected_at"):
        op.create_index(f"ix_experience_episodes_{field}", "experience_episodes", [field])
    op.create_index("ix_experience_episode_family_time", "experience_episodes", ["family", "completed_at"])
    op.create_table(
        "experience_signals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("episode_id", sa.String(36), sa.ForeignKey("experience_episodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for field in ("episode_id", "kind", "source_id"):
        op.create_index(f"ix_experience_signals_{field}", "experience_signals", [field])
    op.create_table(
        "experience_insights",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("insight_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("family", sa.String(64), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("operation", sa.String(12), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("support_count", sa.Integer(), nullable=False),
        sa.Column("refute_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True)),
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("insight_id", "version", name="uq_experience_insight_version"),
    )
    op.create_index("ix_experience_insight_family_status", "experience_insights", ["family", "status"])


def downgrade() -> None:
    for table in ("experience_insights", "experience_signals", "experience_episodes"):
        op.drop_table(table)
