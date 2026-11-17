"""Persist cancellation intent independently from execution outcome."""

from alembic import op
import sqlalchemy as sa

revision = "e6f90123b405"
down_revision = "d5e8f102b304"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("tool_operations", "agent_actions"):
        op.add_column(
            table,
            sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        )
    with op.batch_alter_table("desktop_action_receipts") as batch:
        batch.drop_constraint("ck_desktop_receipt_status", type_="check")
        batch.create_check_constraint(
            "ck_desktop_receipt_status",
            "status IN ('received','started','committed','failed','cancelled','unknown')",
        )
    with op.batch_alter_table("tool_operations") as batch:
        batch.drop_constraint("ck_tool_operation_status", type_="check")
        batch.create_check_constraint(
            "ck_tool_operation_status",
            "status IN ('queued','running','committed','failed','cancelled','execution_unknown')",
        )


def downgrade():
    op.execute(
        "UPDATE desktop_action_receipts SET status='failed' WHERE status='cancelled'"
    )
    with op.batch_alter_table("desktop_action_receipts") as batch:
        batch.drop_constraint("ck_desktop_receipt_status", type_="check")
        batch.create_check_constraint(
            "ck_desktop_receipt_status",
            "status IN ('received','started','committed','failed','unknown')",
        )
    op.execute(
        "UPDATE tool_operations SET status='failed', terminal_reason='cancelled' WHERE status='cancelled'"
    )
    with op.batch_alter_table("tool_operations") as batch:
        batch.drop_constraint("ck_tool_operation_status", type_="check")
        batch.create_check_constraint(
            "ck_tool_operation_status",
            "status IN ('queued','running','committed','failed','execution_unknown')",
        )
    for table in ("tool_operations", "agent_actions"):
        op.drop_column(table, "cancel_requested_at")
