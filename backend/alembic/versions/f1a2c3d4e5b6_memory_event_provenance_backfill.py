"""回填 Memory → Event ProvenanceRef（Forget 来源 Event 隔离）

Revision ID: f1a2c3d4e5b6
Revises: b8d2e6f4a1c7
Create Date: 2026-11-04

背景：ContentProvenanceRef 是 MemoryRecord 与来源 Event 的持久关系，供
Forget 读取侧可见性屏蔽、防重抽与 SegmentSummary 重建长期反查。写入侧
（MemoryWriteService._write_evidence_batch）已改为与 MemoryEvidence 同事务
写入 ProvenanceRef；历史数据中仅有 memory_evidence.source_event_id，缺少
对应 ProvenanceRef，且 MemoryEvidence 会在 Forget purge 阶段被删除。

本迁移把存量 memory_evidence 中非空的 source_event_id 去重回填为
  owner_type=memory_record, owner_id=memory_id,
  source_type=event,          source_id=source_event_id
的 ContentProvenanceRef 行。

约束：
* 幂等：先查已存在的 (owner_id, source_id) 组合，只插入缺失项，重复执行
  upgrade 不会产生重复行，也不会触发 uq_provenance_owner_source。
* 兼容 PostgreSQL / SQLite：UUID 与 created_at 在 Python 侧生成，通过
  SQLAlchemy Core executemany 插入，不依赖任何方言专属函数。
* 不删除任何 MemoryEvidence；不修改 schema（纯数据迁移，无 DDL）。
* downgrade 为 no-op：回填行与应用后续写入的行无法安全区分，删除会破坏
  Forget 来源关系，故不回滚数据。
"""
from datetime import datetime, timezone
import uuid

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2c3d4e5b6"
down_revision: str | None = "b8d2e6f4a1c7"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    evidence = sa.Table("memory_evidence", metadata, autoload_with=bind)
    provenance = sa.Table("content_provenance_refs", metadata, autoload_with=bind)

    pairs = bind.execute(
        sa.select(evidence.c.memory_id, evidence.c.source_event_id)
        .where(evidence.c.source_event_id.isnot(None))
        .distinct()
    ).all()
    if not pairs:
        return

    existing = {
        (row.owner_id, row.source_id)
        for row in bind.execute(
            sa.select(provenance.c.owner_id, provenance.c.source_id).where(
                provenance.c.owner_type == "memory_record",
                provenance.c.source_type == "event",
            )
        ).all()
    }

    now = datetime.now(timezone.utc)
    rows = [
        {
            "id": str(uuid.uuid4()),
            "owner_type": "memory_record",
            "owner_id": memory_id,
            "source_type": "event",
            "source_id": source_event_id,
            "created_at": now,
        }
        for memory_id, source_event_id in pairs
        if source_event_id and (memory_id, source_event_id) not in existing
    ]
    if rows:
        bind.execute(sa.insert(provenance), rows)


def downgrade() -> None:
    # 纯数据回填；回填行与应用写入行不可安全区分，删除会破坏 Forget
    # 来源关系（可见性屏蔽 / 防重抽 / Summary 重建），故不回滚数据。
    pass
