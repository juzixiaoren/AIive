"""新增 Persistent Agent Task Runtime

Revision ID: e2f6a8c4d1b7
Revises: d9a4e7c1b2f0
Create Date: 2026-08-11 12:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e2f6a8c4d1b7"
down_revision: Union[str, Sequence[str], None] = "d9a4e7c1b2f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TASK_STATES = (
    "'queued','dispatching','running','blocked_approval','blocked_user',"
    "'blocked_node','reconciling','verifying','succeeded','partial','failed','cancelled'"
)
ACTION_STATES = (
    "'planned','validated','awaiting_approval','ready','dispatched','running',"
    "'succeeded','failed','cancelled','unknown','invalidated'"
)


def upgrade() -> None:
    op.create_table(
        "agent_tasks",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("source_turn_record_id", sa.String(36), nullable=True),
        sa.Column("task_type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("executor_type", sa.String(32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("target_node_id", sa.String(36), nullable=True),
        sa.Column("task_brief", sa.JSON(), nullable=False),
        sa.Column("task_state", sa.JSON(), nullable=False),
        sa.Column("report", sa.JSON(), nullable=True),
        sa.Column("current_run_id", sa.String(36), nullable=True),
        sa.Column("budgets", sa.JSON(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("wake_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"status IN ({TASK_STATES})", name="ck_agent_task_status"),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_turn_record_id"], ["turn_records.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["target_node_id"], ["desktop_nodes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_tasks_thread_status", "agent_tasks", ["thread_id", "status"])
    op.create_index("ix_agent_tasks_wake", "agent_tasks", ["status", "wake_at"])
    op.create_index("ix_agent_tasks_node_status", "agent_tasks", ["target_node_id", "status"])

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("run_index", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("execution_id", sa.String(36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("output_summary", sa.JSON(), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("token_usage", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('created','running','completed','failed','interrupted')",
            name="ck_agent_run_status",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "run_index", name="uq_agent_run_task_index"),
    )
    op.create_index("ix_agent_runs_task_status", "agent_runs", ["task_id", "status"])

    op.create_table(
        "agent_actions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("execution_run_id", sa.String(36), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("capability_id", sa.String(128), nullable=False),
        sa.Column("target_node_id", sa.String(36), nullable=True),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("descriptor_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        sa.Column("preconditions", sa.JSON(), nullable=False),
        sa.Column("effects", sa.JSON(), nullable=False),
        sa.Column("result_summary", sa.JSON(), nullable=True),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("execution_token", sa.String(36), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"status IN ({ACTION_STATES})", name="ck_agent_action_status"),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["execution_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["target_node_id"], ["desktop_nodes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("task_id", "sequence", name="uq_agent_action_task_sequence"),
    )
    op.create_index("ix_agent_actions_task_status", "agent_actions", ["task_id", "status"])
    op.create_index("ix_agent_actions_node_status", "agent_actions", ["target_node_id", "status"])

    op.create_table(
        "agent_task_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("action_id", sa.String(36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("dedupe_key", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "visibility IN ('conversation','task','internal')",
            name="ck_agent_task_event_visibility",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["action_id"], ["agent_actions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "sequence", name="uq_agent_task_event_sequence"),
        sa.UniqueConstraint("task_id", "dedupe_key", name="uq_agent_task_event_dedupe"),
    )
    op.create_index("ix_agent_task_events_task_created", "agent_task_events", ["task_id", "created_at"])

    op.create_table(
        "agent_task_checkpoints",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("pending_action_refs", sa.JSON(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(128), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "version", name="uq_agent_checkpoint_task_version"),
    )
    op.create_index("ix_agent_checkpoints_task_created", "agent_task_checkpoints", ["task_id", "created_at"])

    op.create_table(
        "task_evidence",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("action_id", sa.String(36), nullable=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("inline_payload", sa.JSON(), nullable=True),
        sa.Column("object_bucket", sa.String(128), nullable=True),
        sa.Column("object_key", sa.String(512), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("content_size", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["action_id"], ["agent_actions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_evidence_task_action", "task_evidence", ["task_id", "action_id"])
    op.create_index("ix_task_evidence_hash", "task_evidence", ["content_hash"])

    op.create_table(
        "task_artifacts",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("action_id", sa.String(36), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("artifact_type", sa.String(64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("object_bucket", sa.String(128), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("content_size", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["action_id"], ["agent_actions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_artifacts_task_created", "task_artifacts", ["task_id", "created_at"])

    op.create_table(
        "task_resource_locks",
        sa.Column("resource_key", sa.String(768), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("action_id", sa.String(36), nullable=True),
        sa.Column("lease_token", sa.String(36), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["action_id"], ["agent_actions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("resource_key"),
    )
    op.create_index("ix_task_resource_locks_task", "task_resource_locks", ["task_id"])
    op.create_index("ix_task_resource_locks_expiry", "task_resource_locks", ["lease_expires_at"])

    op.create_table(
        "agent_task_watches",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("watch_type", sa.String(32), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("cursor", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active','paused','completed')", name="ck_agent_watch_status"),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_task_watches_due", "agent_task_watches", ["status", "next_check_at"])

    op.create_table(
        "desktop_action_receipts",
        sa.Column("action_id", sa.String(36), nullable=False),
        sa.Column("node_id", sa.String(36), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=True),
        sa.Column("result_payload", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("node_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("node_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('received','started','committed','failed','unknown')",
            name="ck_desktop_receipt_status",
        ),
        sa.ForeignKeyConstraint(["action_id"], ["agent_actions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["desktop_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("action_id"),
        sa.UniqueConstraint("node_id", "idempotency_key", name="uq_desktop_receipt_node_idempotency"),
    )
    op.create_index("ix_desktop_receipts_node_status", "desktop_action_receipts", ["node_id", "status"])

    # 现有 Turn 审批升级为 Turn/Task 双模式。batch 模式兼容 SQLite 测试迁移。
    with op.batch_alter_table("approval_requests") as batch:
        batch.alter_column("turn_record_id", existing_type=sa.String(36), nullable=True)
        batch.alter_column("turn_id", existing_type=sa.String(36), nullable=True)
        batch.alter_column("trace_id", existing_type=sa.String(36), nullable=True)
        batch.alter_column("tool_call_id", existing_type=sa.String(128), nullable=True)
        batch.add_column(sa.Column("task_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("action_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("checkpoint_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("preconditions", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("effects", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("approval_hash", sa.String(64), nullable=False, server_default=""))
        batch.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key("fk_approval_task", "agent_tasks", ["task_id"], ["id"], ondelete="CASCADE")
        batch.create_foreign_key("fk_approval_action", "agent_actions", ["action_id"], ["id"], ondelete="CASCADE")
        batch.create_foreign_key(
            "fk_approval_checkpoint", "agent_task_checkpoints", ["checkpoint_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_unique_constraint("uq_approval_action", ["action_id"])
        batch.create_check_constraint(
            "ck_approval_exactly_one_owner",
            "((turn_record_id IS NOT NULL AND action_id IS NULL AND task_id IS NULL) "
            "OR (turn_record_id IS NULL AND action_id IS NOT NULL AND task_id IS NOT NULL))",
        )
        batch.create_index("ix_approval_task_status", ["task_id", "status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("approval_requests") as batch:
        batch.drop_index("ix_approval_task_status")
        batch.drop_constraint("ck_approval_exactly_one_owner", type_="check")
        batch.drop_constraint("uq_approval_action", type_="unique")
        batch.drop_constraint("fk_approval_checkpoint", type_="foreignkey")
        batch.drop_constraint("fk_approval_action", type_="foreignkey")
        batch.drop_constraint("fk_approval_task", type_="foreignkey")
        for name in (
            "expires_at", "approval_hash", "effects", "preconditions",
            "checkpoint_id", "action_id", "task_id",
        ):
            batch.drop_column(name)
        batch.alter_column("tool_call_id", existing_type=sa.String(128), nullable=False)
        batch.alter_column("trace_id", existing_type=sa.String(36), nullable=False)
        batch.alter_column("turn_id", existing_type=sa.String(36), nullable=False)
        batch.alter_column("turn_record_id", existing_type=sa.String(36), nullable=False)

    for index_name, table_name in (
        ("ix_desktop_receipts_node_status", "desktop_action_receipts"),
        ("ix_agent_task_watches_due", "agent_task_watches"),
        ("ix_task_resource_locks_expiry", "task_resource_locks"),
        ("ix_task_resource_locks_task", "task_resource_locks"),
        ("ix_task_artifacts_task_created", "task_artifacts"),
        ("ix_task_evidence_hash", "task_evidence"),
        ("ix_task_evidence_task_action", "task_evidence"),
        ("ix_agent_checkpoints_task_created", "agent_task_checkpoints"),
        ("ix_agent_task_events_task_created", "agent_task_events"),
        ("ix_agent_actions_node_status", "agent_actions"),
        ("ix_agent_actions_task_status", "agent_actions"),
        ("ix_agent_runs_task_status", "agent_runs"),
        ("ix_agent_tasks_node_status", "agent_tasks"),
        ("ix_agent_tasks_wake", "agent_tasks"),
        ("ix_agent_tasks_thread_status", "agent_tasks"),
    ):
        op.drop_index(index_name, table_name=table_name)
    for table_name in (
        "desktop_action_receipts", "agent_task_watches", "task_resource_locks",
        "task_artifacts", "task_evidence", "agent_task_checkpoints",
        "agent_task_events", "agent_actions", "agent_runs", "agent_tasks",
    ):
        op.drop_table(table_name)
