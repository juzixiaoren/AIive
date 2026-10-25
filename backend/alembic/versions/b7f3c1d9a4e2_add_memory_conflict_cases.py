"""长期记忆冲突待确认案例表（memory_conflict_cases）

冲突处理闭环的持久化状态：新提案与既有记忆语义冲突、LLM 无法可靠裁决时，
把双方 id / 版本 / 证据 / 图谱关系与裁决原因落成一条待确认案例，等用户
自然语言回复后再经 MemoryWriteService / Forget Saga 正式写入。

本表不是记忆真相源：
- pending 期间不写 memory_records、不写 graph_relations，现有记忆与关系不变；
- proposal_payload 只是提案快照，重建正式记录仍必须走 MemoryGate +
  ConflictResolver + MemoryWriteService。
"""
import sqlalchemy as sa
from alembic import op

revision = "b7f3c1d9a4e2"
down_revision = "e7a1c4d2f9b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_conflict_cases",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False, server_default="global"),
        sa.Column("scope_id", sa.String(length=128), nullable=True),
        sa.Column("canonical_key", sa.String(length=256), nullable=False),
        sa.Column("memory_type", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("sensitivity", sa.String(length=32), nullable=False, server_default="normal"),
        sa.Column("old_memory_id", sa.String(length=36), nullable=True),
        sa.Column("old_record_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("old_content", sa.Text(), nullable=False, server_default=""),
        sa.Column("new_content", sa.Text(), nullable=False, server_default=""),
        sa.Column("proposal_id", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("proposal_payload", sa.JSON(), nullable=True),
        sa.Column("verdict", sa.String(length=32), nullable=False, server_default="uncertain"),
        sa.Column("verdict_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("graph_evidence", sa.JSON(), nullable=True),
        sa.Column("question", sa.Text(), nullable=False, server_default=""),
        sa.Column("question_event_id", sa.String(length=36), nullable=True),
        sa.Column("clarify_rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decision", sa.String(length=32), nullable=True),
        sa.Column("decision_source", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("decision_span", sa.Text(), nullable=False, server_default=""),
        sa.Column("resolved_memory_id", sa.String(length=36), nullable=True),
        sa.Column("resolution_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_memory_conflict_cases_idempotency"),
        sa.CheckConstraint(
            "status IN ('pending','asking','resolved','rejected','stale','expired')",
            name="ck_memory_conflict_cases_status",
        ),
    )
    op.create_index("ix_memory_conflict_cases_status", "memory_conflict_cases", ["status"])
    op.create_index("ix_memory_conflict_cases_thread_id", "memory_conflict_cases", ["thread_id"])
    op.create_index("ix_memory_conflict_cases_canonical_key", "memory_conflict_cases", ["canonical_key"])
    op.create_index("ix_memory_conflict_cases_old_memory_id", "memory_conflict_cases", ["old_memory_id"])
    op.create_index(
        "ix_memory_conflict_cases_thread_status", "memory_conflict_cases", ["thread_id", "status"],
    )
    op.create_index(
        "ix_memory_conflict_cases_status_expiry", "memory_conflict_cases", ["status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_conflict_cases_status_expiry", table_name="memory_conflict_cases")
    op.drop_index("ix_memory_conflict_cases_thread_status", table_name="memory_conflict_cases")
    op.drop_index("ix_memory_conflict_cases_old_memory_id", table_name="memory_conflict_cases")
    op.drop_index("ix_memory_conflict_cases_canonical_key", table_name="memory_conflict_cases")
    op.drop_index("ix_memory_conflict_cases_thread_id", table_name="memory_conflict_cases")
    op.drop_index("ix_memory_conflict_cases_status", table_name="memory_conflict_cases")
    op.drop_table("memory_conflict_cases")
