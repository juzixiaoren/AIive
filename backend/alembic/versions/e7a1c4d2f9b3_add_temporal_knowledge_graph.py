"""时序知识图谱投影（graph_entities / graph_relations + memory_records.graph_facts）

图谱是 memory_records 的派生层，不是第二套记忆真相源：

- memory_records.graph_facts 保存提取阶段产出的结构化关系事实（canonical），
  随记录版本演进；图谱可由此列完整重建，无需重新调用 LLM。
- graph_entities 保存实体规范化结果（同一 Scope 内 normalized_name 唯一）。
- graph_relations 保存带来源记忆、来源版本、置信度、事实有效期（valid_from/
  valid_to）与系统记录时间（recorded_at）的三元组；relation_key 唯一，
  保证 Outbox 重试与并发投影幂等。

不引入 Neo4j/Graphiti：PostgreSQL/SQLite 现有能力足够，读侧按来源记忆回查
lifecycle / validity / 遗忘状态，失效关系不会被当作当前事实注入。
"""
import sqlalchemy as sa
from alembic import op

revision = "e7a1c4d2f9b3"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_records",
        sa.Column("graph_facts", sa.JSON(), nullable=True),
    )

    op.create_table(
        "graph_entities",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("normalized_name", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("aliases", sa.JSON(), nullable=True),
        sa.Column("scope_type", sa.String(length=32), nullable=False, server_default="global"),
        sa.Column("scope_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "normalized_name", "scope_type", "scope_id",
            name="uq_graph_entities_identity",
        ),
    )
    op.create_index("ix_graph_entities_normalized_name", "graph_entities", ["normalized_name"])
    op.create_index("ix_graph_entities_scope", "graph_entities", ["scope_type", "scope_id"])

    op.create_table(
        "graph_relations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("relation_key", sa.String(length=64), nullable=False, unique=True),
        sa.Column(
            "subject_entity_id", sa.String(length=36),
            sa.ForeignKey("graph_entities.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "object_entity_id", sa.String(length=36),
            sa.ForeignKey("graph_entities.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("predicate", sa.String(length=64), nullable=False),
        sa.Column(
            "source_memory_id", sa.String(length=36),
            sa.ForeignKey("memory_records.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("source_record_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("scope_type", sa.String(length=32), nullable=False, server_default="global"),
        sa.Column("scope_id", sa.String(length=128), nullable=True),
        # Mapped[float] 在 ORM metadata 中映射为 Double，迁移显式声明 Double 以避免
        # SQLite 上 alembic check 报 FLOAT() -> Double() 的类型漂移（PG 上两者等价）。
        sa.Column("confidence", sa.Double(), nullable=False, server_default="0.5"),
        sa.Column("source_span", sa.Text(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("closed_reason", sa.String(length=48), nullable=True),
        sa.Column("superseded_by_id", sa.String(length=36), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active','closed','retracted')",
            name="ck_graph_relation_status",
        ),
    )
    op.create_index("ix_graph_relations_predicate", "graph_relations", ["predicate"])
    op.create_index("ix_graph_relations_source_memory_id", "graph_relations", ["source_memory_id"])
    op.create_index(
        "ix_graph_relations_subject_status", "graph_relations",
        ["subject_entity_id", "status"],
    )
    op.create_index(
        "ix_graph_relations_object_status", "graph_relations",
        ["object_entity_id", "status"],
    )
    op.create_index(
        "ix_graph_relations_source_version", "graph_relations",
        ["source_memory_id", "source_record_version"],
    )


def downgrade() -> None:
    op.drop_index("ix_graph_relations_source_version", table_name="graph_relations")
    op.drop_index("ix_graph_relations_object_status", table_name="graph_relations")
    op.drop_index("ix_graph_relations_subject_status", table_name="graph_relations")
    op.drop_index("ix_graph_relations_source_memory_id", table_name="graph_relations")
    op.drop_index("ix_graph_relations_predicate", table_name="graph_relations")
    op.drop_table("graph_relations")
    op.drop_index("ix_graph_entities_scope", table_name="graph_entities")
    op.drop_index("ix_graph_entities_normalized_name", table_name="graph_entities")
    op.drop_table("graph_entities")
    op.drop_column("memory_records", "graph_facts")
