"""Phase 0.5B: OutboxJob claim/lease columns, MemoryIngestionRun, MemoryProposal ingestion columns.

Schema → Data migration order:
1. OutboxJob new columns + indexes
2. MemoryIngestionRun table
3. MemoryProposal new columns + unique constraint
4. Migration audit table
5. Historical Job migration

Revision ID: c8d9e0f1a2b3
Revises: b5e6f7a8c9d0
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c8d9e0f1a2b3"
down_revision: Union[str, Sequence[str], None] = "b5e6f7a8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MIGRATION_BATCH_ID = "phase05b_migration_v6_001"


def upgrade() -> None:
    bind = op.get_bind()

    # ==================================================================
    # Schema changes
    # ==================================================================

    # 1. OutboxJob new columns
    op.add_column("outbox_jobs", sa.Column("locked_by", sa.String(64), nullable=True))
    op.add_column("outbox_jobs", sa.Column("claim_token", sa.String(36), nullable=True))
    op.add_column("outbox_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox_jobs", sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("outbox_jobs", sa.Column("available_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox_jobs", sa.Column("terminal_reason", sa.String(64), nullable=True))
    op.add_column("outbox_jobs", sa.Column("original_status", sa.String(32), nullable=True))
    op.add_column("outbox_jobs", sa.Column("migration_batch_id", sa.String(36), nullable=True))
    op.create_index("ix_outbox_claim", "outbox_jobs", ["status", "available_at", "lease_expires_at"])
    op.create_index("ix_outbox_migration_batch", "outbox_jobs", ["migration_batch_id"])

    # 2. MemoryIngestionRun table
    op.create_table(
        "memory_ingestion_runs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("source_turn_record_id", sa.String(36), nullable=False),
        sa.Column("extractor_name", sa.String(64), nullable=False),
        sa.Column("extractor_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("execution_token", sa.String(36), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("proposal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["source_turn_record_id"], ["turn_records.id"]),
        sa.UniqueConstraint("source_turn_record_id", "extractor_name", "extractor_version",
                            name="uq_ingestion_run_source_extractor"),
    )

    # 3. MemoryProposal new columns
    op.add_column("memory_proposals", sa.Column("source_turn_id", sa.String(36), nullable=True))
    op.add_column("memory_proposals", sa.Column("ingestion_run_id", sa.String(36), nullable=True))
    op.add_column("memory_proposals", sa.Column("proposal_index", sa.Integer(), nullable=True))
    op.create_unique_constraint("uq_ingestion_run_proposal_index", "memory_proposals",
                                ["ingestion_run_id", "proposal_index"])

    # 4. Migration audit table
    op.create_table(
        "outbox_migration_audit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("migration_batch_id", sa.String(36), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("job_type", sa.String(64), nullable=True),
        sa.Column("original_status", sa.String(32), nullable=True),
        sa.Column("new_status", sa.String(32), nullable=True),
        sa.Column("terminal_reason", sa.String(64), nullable=True),
        sa.Column("migrated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("migration_batch_id", "job_id", name="uq_migration_audit_batch_job"),
    )

    # ==================================================================
    # Data migration
    # ==================================================================

    # Step 1: Cancel orphaned running jobs
    bind.execute(sa.text("""
        WITH affected AS (
            UPDATE outbox_jobs
            SET status = 'deadletter',
                terminal_reason = 'cancelled_legacy',
                migration_batch_id = :batch_id,
                original_status = status,
                error_message = 'cancelled: running job without active worker',
                locked_by = NULL, claim_token = NULL, lease_expires_at = NULL,
                updated_at = NOW()
            WHERE status = 'running'
            RETURNING id, job_type, 'running' AS orig_status
        )
        INSERT INTO outbox_migration_audit
            (migration_batch_id, job_id, job_type, original_status, new_status, terminal_reason)
        SELECT :batch_id, id, job_type, orig_status, 'deadletter', 'cancelled_legacy'
        FROM affected
        ON CONFLICT (migration_batch_id, job_id) DO NOTHING
    """), {"batch_id": MIGRATION_BATCH_ID})

    # Step 2: Cancel historical extraction without source_turn_record_id
    bind.execute(sa.text("""
        WITH affected AS (
            UPDATE outbox_jobs
            SET status = 'deadletter',
                terminal_reason = 'cancelled_legacy',
                migration_batch_id = :batch_id,
                original_status = status,
                error_message = 'cancelled: historical extraction (missing source_turn_record_id)',
                locked_by = NULL, claim_token = NULL, lease_expires_at = NULL,
                updated_at = NOW()
            WHERE status = 'pending'
              AND job_type IN ('memory_extraction', 'steward_extraction')
              AND (payload->>'source_turn_record_id' IS NULL
                   OR payload->>'source_turn_record_id' = '')
            RETURNING id, job_type, 'pending' AS orig_status
        )
        INSERT INTO outbox_migration_audit
            (migration_batch_id, job_id, job_type, original_status, new_status, terminal_reason)
        SELECT :batch_id, id, job_type, orig_status, 'deadletter', 'cancelled_legacy'
        FROM affected
        ON CONFLICT (migration_batch_id, job_id) DO NOTHING
    """), {"batch_id": MIGRATION_BATCH_ID})

    # Step 3: Cancel all remaining pending steward_extraction
    bind.execute(sa.text("""
        WITH affected AS (
            UPDATE outbox_jobs
            SET status = 'deadletter',
                terminal_reason = 'cancelled_legacy',
                migration_batch_id = :batch_id,
                original_status = status,
                error_message = 'cancelled: steward_extraction not in allowlist',
                locked_by = NULL, claim_token = NULL, lease_expires_at = NULL,
                updated_at = NOW()
            WHERE status = 'pending'
              AND job_type = 'steward_extraction'
            RETURNING id, job_type, 'pending' AS orig_status
        )
        INSERT INTO outbox_migration_audit
            (migration_batch_id, job_id, job_type, original_status, new_status, terminal_reason)
        SELECT :batch_id, id, job_type, orig_status, 'deadletter', 'cancelled_legacy'
        FROM affected
        ON CONFLICT (migration_batch_id, job_id) DO NOTHING
    """), {"batch_id": MIGRATION_BATCH_ID})

    # Step 4: Quarantine non-allowlist pending jobs
    bind.execute(sa.text("""
        WITH affected AS (
            UPDATE outbox_jobs
            SET status = 'deadletter',
                terminal_reason = CASE
                    WHEN job_type IN ('memory_vector_upsert', 'memory_vector_delete',
                                      'memory_markdown_project', 'memory_cache_invalidate')
                        THEN 'unsupported_handler'
                    ELSE 'quarantined_unknown'
                END,
                migration_batch_id = :batch_id,
                original_status = status,
                error_message = 'quarantined: job_type not in allowlist',
                locked_by = NULL, claim_token = NULL, lease_expires_at = NULL,
                updated_at = NOW()
            WHERE status = 'pending'
              AND job_type NOT IN ('memory_extraction', 'core_memory_refresh')
            RETURNING id, job_type, 'pending' AS orig_status
        )
        INSERT INTO outbox_migration_audit
            (migration_batch_id, job_id, job_type, original_status, new_status, terminal_reason)
        SELECT :batch_id, id, job_type, orig_status, 'deadletter',
               CASE
                   WHEN job_type IN ('memory_vector_upsert', 'memory_vector_delete',
                                     'memory_markdown_project', 'memory_cache_invalidate')
                       THEN 'unsupported_handler'
                   ELSE 'quarantined_unknown'
               END
        FROM affected
        ON CONFLICT (migration_batch_id, job_id) DO NOTHING
    """), {"batch_id": MIGRATION_BATCH_ID})

    # Step 5: Set defaults
    bind.execute(sa.text("""
        UPDATE outbox_jobs
        SET schema_version = COALESCE(schema_version, 1),
            available_at = COALESCE(available_at, created_at)
        WHERE schema_version IS NULL OR available_at IS NULL
    """))


def downgrade() -> None:
    bind = op.get_bind()

    # 1. Restore jobs from audit
    bind.execute(sa.text("""
        UPDATE outbox_jobs o
        SET status = a.original_status,
            terminal_reason = NULL,
            migration_batch_id = NULL,
            original_status = NULL,
            error_message = NULL,
            updated_at = NOW()
        FROM outbox_migration_audit a
        WHERE o.id = a.job_id
          AND a.migration_batch_id = :batch_id
    """), {"batch_id": MIGRATION_BATCH_ID})

    # 2. Delete audit records
    bind.execute(sa.text("""
        DELETE FROM outbox_migration_audit
        WHERE migration_batch_id = :batch_id
    """), {"batch_id": MIGRATION_BATCH_ID})

    # 3. Drop MemoryProposal unique constraint and columns
    op.drop_constraint("uq_ingestion_run_proposal_index", "memory_proposals")
    op.drop_column("memory_proposals", "proposal_index")
    op.drop_column("memory_proposals", "ingestion_run_id")
    op.drop_column("memory_proposals", "source_turn_id")

    # 4. Drop MemoryIngestionRun table
    op.drop_table("memory_ingestion_runs")

    # 5. Drop OutboxJob indexes and columns
    op.drop_index("ix_outbox_claim", table_name="outbox_jobs")
    op.drop_index("ix_outbox_migration_batch", table_name="outbox_jobs")
    op.drop_column("outbox_jobs", "migration_batch_id")
    op.drop_column("outbox_jobs", "original_status")
    op.drop_column("outbox_jobs", "terminal_reason")
    op.drop_column("outbox_jobs", "available_at")
    op.drop_column("outbox_jobs", "schema_version")
    op.drop_column("outbox_jobs", "lease_expires_at")
    op.drop_column("outbox_jobs", "claim_token")
    op.drop_column("outbox_jobs", "locked_by")

    # 6. Drop audit table last
    op.drop_table("outbox_migration_audit")
