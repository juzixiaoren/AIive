"""Phase 3: Segment Sealing / CompactionInput / EpochCheckpoint 运行记录与约束。

- 新增表：compaction_inputs / compaction_runs / epoch_compaction_inputs / checkpoint_runs
- segment_summaries 新增列（source_turn_ids / source_event_ids / active_constraints /
  unresolved_failures / omitted_artifact_refs）
- epoch_checkpoints 新增列（source_hashes）
- 新增 UNIQUE 约束与 segments 的 PARTIAL UNIQUE INDEX / CHECK 约束
- 加 CHECK(status<>'sealed' OR summary_id IS NOT NULL) 前执行 fail-closed 审计：
  若存在 sealed+NULL summary_id 的 Segment，迁移直接失败并输出异常 Segment IDs，
  须先运行 scripts/repair_sealed_null_summary.py 修复。

Revision ID: f3a1b2c4d5e6
Revises: 0205b4c1e
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f3a1b2c4d5e6"
down_revision: Union[str, Sequence[str], None] = "0205b4c1e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(conn, table: str, column: str) -> bool:
    try:
        insp = sa.inspect(conn)
        cols = [c["name"] for c in insp.get_columns(table)]
        return column in cols
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # ── 新表：compaction_inputs ──
    op.create_table(
        "compaction_inputs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("segment_id", sa.String(36), sa.ForeignKey("segments.id"), nullable=False),
        sa.Column("start_turn_sequence", sa.BigInteger(), nullable=False),
        sa.Column("end_turn_sequence", sa.BigInteger(), nullable=False),
        sa.Column("turn_manifest", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("event_manifest", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("working_state_version", sa.Integer(), nullable=False),
        sa.Column("working_state_snapshot", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("summary_version", sa.Integer(), server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_compaction_inputs_segment", "compaction_inputs", ["segment_id"])
    op.create_unique_constraint(
        "uq_compaction_input_segment_hash_version", "compaction_inputs",
        ["segment_id", "source_hash", "summary_version"],
    )

    # ── 新表：compaction_runs ──
    op.create_table(
        "compaction_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("segment_id", sa.String(36), sa.ForeignKey("segments.id"), nullable=False),
        sa.Column("compaction_input_id", sa.String(36), sa.ForeignKey("compaction_inputs.id"), nullable=False),
        sa.Column("outbox_job_id", sa.String(36), sa.ForeignKey("outbox_jobs.id"), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("summary_version", sa.Integer(), server_default=sa.text("1")),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'running'")),
        sa.Column("execution_token", sa.String(128), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_compaction_segment_status", "compaction_runs", ["segment_id", "status"])
    op.create_unique_constraint(
        "uq_compaction_segment_hash_version", "compaction_runs",
        ["segment_id", "source_hash", "summary_version"],
    )
    op.create_unique_constraint(
        "uq_compaction_run_outbox_job", "compaction_runs", ["outbox_job_id"],
    )

    # ── 新表：epoch_compaction_inputs ──
    op.create_table(
        "epoch_compaction_inputs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("epoch_id", sa.String(36), sa.ForeignKey("epochs.id"), nullable=False),
        sa.Column("boundary_turn_sequence", sa.BigInteger(), nullable=False),
        sa.Column("working_state_version", sa.Integer(), nullable=False),
        sa.Column("current_objective", sa.Text(), nullable=True),
        sa.Column("open_loops", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("active_constraints", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("artifact_refs", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("verified_tool_states", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("source_segment_ids", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("source_hashes", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("checkpoint_version", sa.Integer(), server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_epoch_compaction_inputs_epoch", "epoch_compaction_inputs", ["epoch_id"])
    op.create_unique_constraint(
        "uq_epoch_input_epoch_hash_version", "epoch_compaction_inputs",
        ["epoch_id", "snapshot_hash", "checkpoint_version"],
    )

    # ── 新表：checkpoint_runs ──
    op.create_table(
        "checkpoint_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("epoch_id", sa.String(36), sa.ForeignKey("epochs.id"), nullable=False),
        sa.Column("epoch_compaction_input_id", sa.String(36), sa.ForeignKey("epoch_compaction_inputs.id"), nullable=False),
        sa.Column("outbox_job_id", sa.String(36), sa.ForeignKey("outbox_jobs.id"), nullable=False),
        sa.Column("boundary_hash", sa.String(64), nullable=False),
        sa.Column("checkpoint_version", sa.Integer(), server_default=sa.text("1")),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'running'")),
        sa.Column("execution_token", sa.String(128), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_checkpoint_epoch_status", "checkpoint_runs", ["epoch_id", "status"])
    op.create_unique_constraint(
        "uq_checkpoint_epoch_hash_version", "checkpoint_runs",
        ["epoch_id", "boundary_hash", "checkpoint_version"],
    )
    op.create_unique_constraint(
        "uq_checkpoint_run_outbox_job", "checkpoint_runs", ["outbox_job_id"],
    )

    # ── segment_summaries 新增列 ──
    for col, ctype in (
        ("source_turn_ids", postgresql.JSONB if is_pg else sa.JSON()),
        ("source_event_ids", postgresql.JSONB if is_pg else sa.JSON()),
        ("active_constraints", postgresql.JSONB if is_pg else sa.JSON()),
        ("unresolved_failures", postgresql.JSONB if is_pg else sa.JSON()),
        ("omitted_artifact_refs", postgresql.JSONB if is_pg else sa.JSON()),
    ):
        if not _column_exists(bind, "segment_summaries", col):
            op.add_column("segment_summaries", sa.Column(col, ctype, nullable=True))
    op.create_unique_constraint(
        "uq_segment_summary_segment_version", "segment_summaries",
        ["segment_id", "summary_version"],
    )

    # ── epoch_checkpoints 新增列 ──
    if not _column_exists(bind, "epoch_checkpoints", "source_hashes"):
        op.add_column(
            "epoch_checkpoints",
            sa.Column("source_hashes", postgresql.JSONB if is_pg else sa.JSON(), nullable=True),
        )
    op.create_unique_constraint(
        "uq_epoch_checkpoint_epoch_version", "epoch_checkpoints",
        ["epoch_id", "version"],
    )

    # ── segments：PARTIAL UNIQUE INDEX(thread_id) WHERE status='sealing' ──
    if is_pg:
        op.execute(sa.text(
            "CREATE UNIQUE INDEX uq_segment_thread_sealing "
            "ON segments (thread_id) WHERE status = 'sealing'"
        ))

    # ── L.4 fail-closed 审计：sealed + summary_id IS NULL ──
    result = bind.execute(sa.text(
        "SELECT id FROM segments WHERE status = 'sealed' AND summary_id IS NULL"
    ))
    bad_ids = [r[0] for r in result]
    if bad_ids:
        raise RuntimeError(
            "FAIL-CLOSED: 存在 sealed 但 summary_id 为 NULL 的 Segment，"
            "禁止直接添加 CHECK 约束。请先运行 scripts/repair_sealed_null_summary.py 修复。"
            f"异常 Segment IDs: {bad_ids}"
        )

    # ── segments：CHECK(status<>'sealed' OR summary_id IS NOT NULL) ──
    op.create_check_constraint(
        "ck_segment_sealed_requires_summary", "segments",
        sa.text("status <> 'sealed' OR summary_id IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    op.drop_constraint("ck_segment_sealed_requires_summary", "segments", type_="check")
    if is_pg:
        op.execute(sa.text("DROP INDEX IF EXISTS uq_segment_thread_sealing"))

    op.drop_constraint("uq_epoch_checkpoint_epoch_version", "epoch_checkpoints", type_="unique")
    if _column_exists(bind, "epoch_checkpoints", "source_hashes"):
        op.drop_column("epoch_checkpoints", "source_hashes")

    op.drop_constraint("uq_segment_summary_segment_version", "segment_summaries", type_="unique")
    for col in ("omitted_artifact_refs", "unresolved_failures", "active_constraints",
                "source_event_ids", "source_turn_ids"):
        if _column_exists(bind, "segment_summaries", col):
            op.drop_column("segment_summaries", col)

    op.drop_table("checkpoint_runs")
    op.drop_table("epoch_compaction_inputs")
    op.drop_table("compaction_runs")
    op.drop_table("compaction_inputs")
