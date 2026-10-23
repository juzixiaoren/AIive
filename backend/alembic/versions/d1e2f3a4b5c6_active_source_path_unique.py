"""知识库活跃文档 source_path 唯一性（根治并发导入同路径竞态）

同一 source_path 同时只允许一条活跃文档（indexed / index_failed /
source_unavailable）。此前仅靠 `ingest` 的应用层"先查后写"无法在
READ COMMITTED 下拦截并发：两个事务未提交插入互不可见，最终会同时留下
两个活跃版本，导致同一路径的新旧内容同时被召回。部分唯一索引把该不变量
下沉到 schema；失效态（superseded / deleted）不占用唯一位，版本切换与
历史版本复活不受影响。

升级前先清理历史重复：V39 之前"同路径改内容"会留下新旧两份活跃文档，
直接建唯一索引会失败。每个 source_path 保留 created_at 最新（同日按 id
字典序）的一条，其余置 superseded 并删除其分块（旧内容不再参与检索；
原文对象仍保留，后续可由维护任务回收）。downgrade 只移除索引，被置
失效的历史重复行不会自动恢复（数据安全优先）。
"""
from alembic import op
from sqlalchemy import text

revision = "d1e2f3a4b5c6"
down_revision = "c7e3a1f5b9d2"
branch_labels = None
depends_on = None


_ACTIVE_STATUSES = "('indexed', 'index_failed', 'source_unavailable')"

# 每个 source_path 保留最新一条活跃文档，其余为待置失效的历史重复行
_DUPLICATE_IDS_SQL = f"""
SELECT id FROM (
    SELECT id, ROW_NUMBER() OVER (
        PARTITION BY source_path
        ORDER BY created_at DESC, id DESC
    ) AS rn
    FROM documents
    WHERE status IN {_ACTIVE_STATUSES}
) ranked
WHERE rn > 1
"""


def upgrade() -> None:
    # 1) 历史重复活跃文档：先删分块（维持"失效文档分块必须为空"），再置 superseded
    op.execute(f"DELETE FROM chunks WHERE document_id IN ({_DUPLICATE_IDS_SQL})")
    op.execute(f"UPDATE documents SET status = 'superseded' WHERE id IN ({_DUPLICATE_IDS_SQL})")
    # 2) 活跃文档 source_path 部分唯一索引（SQLite / PostgreSQL 均支持部分索引）
    op.create_index(
        "ix_documents_active_source_path",
        "documents",
        ["source_path"],
        unique=True,
        sqlite_where=text(f"status IN {_ACTIVE_STATUSES}"),
        postgresql_where=text(f"status IN {_ACTIVE_STATUSES}"),
    )


def downgrade() -> None:
    op.drop_index("ix_documents_active_source_path", table_name="documents")
