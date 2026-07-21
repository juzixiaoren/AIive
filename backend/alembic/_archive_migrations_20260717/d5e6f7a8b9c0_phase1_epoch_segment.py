"""Phase 1: Epoch, Segment, WorkingState, Artifact + ContextSnapshot/TurnRecord/LLMCall additions.

- New tables: epochs, segments, segment_summaries, epoch_checkpoints, working_states, artifacts
- Modified tables: turn_records (+epoch_id, +segment_id), context_snapshots (+epoch_id, +segment_id, +turn_sequence, +retention, +token_total), llm_calls (+estimated_prompt_tokens, +safe_prompt_tokens, +actual_prompt_tokens, +actual_completion_tokens, +token_source)
- Partial unique indexes for Epoch/Segment concurrency

Revision ID: d5e6f7a8b9c0
Revises: c8d9e0f1a2b3
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, Sequence[str], None] = 'c8d9e0f1a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # ── epochs ──
    op.create_table(
        "epochs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("thread_id", sa.String(36), sa.ForeignKey("threads.id"), nullable=False),
        sa.Column("epoch_no", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'active'")),
        sa.Column("start_turn_sequence", sa.BigInteger(), nullable=True),
        sa.Column("end_turn_sequence", sa.BigInteger(), nullable=True),
        sa.Column("checkpoint_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_epoch_thread_no", "epochs", ["thread_id", "epoch_no"])
    op.create_index("ix_epochs_thread_status", "epochs", ["thread_id", "status"])

    # Partial unique: max 1 active Epoch per thread
    if is_pg:
        op.execute(sa.text(
            "CREATE UNIQUE INDEX uq_epoch_thread_active ON epochs (thread_id) WHERE status = 'active'"
        ))

    # ── segments ──
    op.create_table(
        "segments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("epoch_id", sa.String(36), sa.ForeignKey("epochs.id"), nullable=False),
        sa.Column("thread_id", sa.String(36), sa.ForeignKey("threads.id"), nullable=False),
        sa.Column("segment_no", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'open'")),
        sa.Column("start_turn_sequence", sa.BigInteger(), nullable=False),
        sa.Column("end_turn_sequence", sa.BigInteger(), nullable=True),
        sa.Column("source_hash", sa.String(64), nullable=True),
        sa.Column("summary_id", sa.String(36), nullable=True),
        sa.Column("pending_seal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sealed_by_turn", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_segment_epoch_no", "segments", ["epoch_id", "segment_no"])
    op.create_index("ix_segments_epoch_status", "segments", ["epoch_id", "status"])

    if is_pg:
        op.execute(sa.text(
            "CREATE UNIQUE INDEX uq_segment_epoch_open ON segments (epoch_id) WHERE status = 'open'"
        ))

    # ── segment_summaries ──
    op.create_table(
        "segment_summaries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("segment_id", sa.String(36), nullable=False),
        sa.Column("goal", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("decisions", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("open_loops", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("entities", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("artifacts", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("important_tool_results", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("source_turn_range", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("source_hash", sa.String(64), nullable=True),
        sa.Column("summary_version", sa.Integer(), server_default=sa.text("1")),
        sa.Column("model_id", sa.String(128), nullable=True),
        sa.Column("token_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_segment_summaries_segment", "segment_summaries", ["segment_id"])

    # ── epoch_checkpoints ──
    op.create_table(
        "epoch_checkpoints",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("epoch_id", sa.String(36), nullable=False),
        sa.Column("current_goal", sa.Text(), nullable=True),
        sa.Column("completed_milestones", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("open_loops", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("active_constraints", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("current_decisions", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("referenced_artifacts", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("relevant_entities", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("latest_verified_tool_states", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("source_segment_ids", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1")),
        sa.Column("token_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_epoch_checkpoints_epoch", "epoch_checkpoints", ["epoch_id"])

    # ── working_states ──
    op.create_table(
        "working_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("thread_id", sa.String(36), sa.ForeignKey("threads.id"), nullable=False),
        sa.Column("epoch_id", sa.String(36), nullable=True),
        sa.Column("current_objective", sa.Text(), nullable=True),
        sa.Column("open_loops", postgresql.JSONB if is_pg else sa.JSON(), server_default=sa.text("'[]'")),
        sa.Column("active_constraints", postgresql.JSONB if is_pg else sa.JSON(), server_default=sa.text("'[]'")),
        sa.Column("pending_approvals", postgresql.JSONB if is_pg else sa.JSON(), server_default=sa.text("'[]'")),
        sa.Column("artifact_refs", postgresql.JSONB if is_pg else sa.JSON(), server_default=sa.text("'[]'")),
        sa.Column("verified_tool_states", postgresql.JSONB if is_pg else sa.JSON(), server_default=sa.text("'[]'")),
        sa.Column("uncommitted_side_effects", postgresql.JSONB if is_pg else sa.JSON(), server_default=sa.text("'[]'")),
        sa.Column("running_tool_state", postgresql.JSONB if is_pg else sa.JSON(), server_default=sa.text("'[]'")),
        sa.Column("applied_idempotency_keys", postgresql.JSONB if is_pg else sa.JSON(), server_default=sa.text("'[]'")),
        sa.Column("token_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("version", sa.Integer(), server_default=sa.text("1")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_unique_constraint("uq_working_state_thread", "working_states", ["thread_id"])

    # ── artifacts ──
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("thread_id", sa.String(36), sa.ForeignKey("threads.id"), nullable=False),
        sa.Column("trace_id", sa.String(36), nullable=True),
        sa.Column("kind", sa.String(64), server_default=sa.text("'tool_result'")),
        sa.Column("ref", sa.String(256), unique=True, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("token_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("turn_record_id", sa.String(36), nullable=True),
        sa.Column("execution_id", sa.String(36), nullable=True),
        sa.Column("tool_call_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_artifacts_thread", "artifacts", ["thread_id"])
    op.create_index("ix_artifacts_ref", "artifacts", ["ref"])
    op.create_unique_constraint("uq_artifact_turn_tool", "artifacts", ["turn_record_id", "tool_call_id"])

    # ── turn_records: add epoch_id, segment_id (nullable) ──
    op.add_column("turn_records", sa.Column("epoch_id", sa.String(36), nullable=True))
    op.add_column("turn_records", sa.Column("segment_id", sa.String(36), nullable=True))

    # ── context_snapshots: add Phase 1 columns ──
    op.add_column("context_snapshots", sa.Column("epoch_id", sa.String(36), nullable=True))
    op.add_column("context_snapshots", sa.Column("segment_id", sa.String(36), nullable=True))
    op.add_column("context_snapshots", sa.Column("turn_sequence", sa.BigInteger(), nullable=True))
    op.add_column("context_snapshots", sa.Column("retention", sa.String(32), server_default=sa.text("'temporary'")))
    op.add_column("context_snapshots", sa.Column("token_total", sa.Integer(), server_default=sa.text("0")))
    op.create_index("ix_snapshots_thread_retention", "context_snapshots", ["thread_id", "retention"])

    if is_pg:
        op.execute(sa.text(
            "CREATE UNIQUE INDEX uq_snapshot_thread_current "
            "ON context_snapshots (thread_id, retention) WHERE retention = 'current'"
        ))

    # ── llm_calls: add token estimation columns ──
    op.add_column("llm_calls", sa.Column("estimated_prompt_tokens", sa.Integer(), nullable=True))
    op.add_column("llm_calls", sa.Column("safe_prompt_tokens", sa.Integer(), nullable=True))
    op.add_column("llm_calls", sa.Column("actual_prompt_tokens", sa.Integer(), nullable=True))
    op.add_column("llm_calls", sa.Column("actual_completion_tokens", sa.Integer(), nullable=True))
    op.add_column("llm_calls", sa.Column("token_source", sa.String(32), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # llm_calls columns
    op.drop_column("llm_calls", "token_source")
    op.drop_column("llm_calls", "actual_completion_tokens")
    op.drop_column("llm_calls", "actual_prompt_tokens")
    op.drop_column("llm_calls", "safe_prompt_tokens")
    op.drop_column("llm_calls", "estimated_prompt_tokens")

    # context_snapshots columns + indexes
    if is_pg:
        op.execute(sa.text("DROP INDEX IF EXISTS uq_snapshot_thread_current"))
    op.drop_index("ix_snapshots_thread_retention", table_name="context_snapshots")
    op.drop_column("context_snapshots", "token_total")
    op.drop_column("context_snapshots", "retention")
    op.drop_column("context_snapshots", "turn_sequence")
    op.drop_column("context_snapshots", "segment_id")
    op.drop_column("context_snapshots", "epoch_id")

    # turn_records columns
    op.drop_column("turn_records", "segment_id")
    op.drop_column("turn_records", "epoch_id")

    # artifacts
    op.drop_index("ix_artifacts_ref", table_name="artifacts")
    op.drop_index("ix_artifacts_thread", table_name="artifacts")
    op.drop_constraint("uq_artifact_turn_tool", "artifacts")
    op.drop_table("artifacts")

    # working_states
    op.drop_column("working_states", "applied_idempotency_keys")
    op.drop_table("working_states")

    # epoch_checkpoints
    op.drop_index("ix_epoch_checkpoints_epoch", table_name="epoch_checkpoints")
    op.drop_table("epoch_checkpoints")

    # segment_summaries
    op.drop_index("ix_segment_summaries_segment", table_name="segment_summaries")
    op.drop_table("segment_summaries")

    # segments
    if is_pg:
        op.execute(sa.text("DROP INDEX IF EXISTS uq_segment_epoch_open"))
    op.drop_index("ix_segments_epoch_status", table_name="segments")
    op.drop_constraint("uq_segment_epoch_no", "segments")
    op.drop_table("segments")

    # epochs
    if is_pg:
        op.execute(sa.text("DROP INDEX IF EXISTS uq_epoch_thread_active"))
    op.drop_index("ix_epochs_thread_status", table_name="epochs")
    op.drop_constraint("uq_epoch_thread_no", "epochs")
    op.drop_table("epochs")
