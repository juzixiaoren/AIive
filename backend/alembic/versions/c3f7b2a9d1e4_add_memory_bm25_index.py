"""新增记忆 BM25 索引（ParadeDB pg_search）

Revision ID: c3f7b2a9d1e4
Revises: a9d3e5f7b1c4
Create Date: 2026-11-03

索引直接建在权威表 memory_records 上（content / canonical_key / keywords），
pg_search 的 bm25 索引与表数据在同一个事务里维护，因此：

* 不存在第二份记忆事实源；
* 记忆的 INSERT / UPDATE（含 forget 的内容 scrub）/ DELETE / 事务回滚
  都由 PostgreSQL 保证索引与表一致，不需要额外的索引同步任务。

Tokenizer 选择：
* content / keywords → pdb.jieba（中文分词 + 英文词元，适配中英混排）
* canonical_key      → pdb.literal_normalized（整串作为一个词元，保留
  「user.preference.editor_theme」这类标识符的精确匹配能力）

扩展未安装（镜像未带 pg_search、或执行迁移的账号无 superuser）时不阻断升级：
索引建不出来，运行时会显式判定 degraded 并降级为词法召回。
"""
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "c3f7b2a9d1e4"
down_revision: str | Sequence[str] | None = "a9d3e5f7b1c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 backend/aiive/memory/bm25_search.py::BM25_INDEX_NAME 保持一致。
INDEX_NAME = "ix_memory_records_bm25"

INDEX_DDL = f"""
CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON memory_records USING bm25 (
    id,
    (content::pdb.jieba),
    (canonical_key::pdb.literal_normalized),
    ((keywords)::text::pdb.jieba)
)
"""


def _warn(message: str) -> None:
    import logging

    logging.getLogger("alembic.runtime.migration").warning(message)


def upgrade() -> None:
    """安装 pg_search 并建立 memory_records 的 BM25 索引（幂等）。"""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite 等开发库没有 pg_search：迁移保持 no-op，运行时 BM25 状态为
        # unsupported_dialect，召回降级为词法并标记 degraded。
        return

    try:
        # 用 savepoint 隔离：CREATE EXTENSION 失败不会污染整个迁移事务。
        with bind.begin_nested():
            bind.execute(text("CREATE EXTENSION IF NOT EXISTS vector CASCADE"))
            bind.execute(text("CREATE EXTENSION IF NOT EXISTS pg_search CASCADE"))
    except Exception as exc:  # noqa: BLE001 - 扩展缺失属于可降级情形
        _warn(
            "pg_search 扩展不可用，跳过 BM25 索引创建；"
            f"记忆检索将在运行时标记 degraded 并降级为词法召回（{exc}）"
        )
        return

    try:
        with bind.begin_nested():
            bind.execute(text(INDEX_DDL))
    except Exception as exc:  # noqa: BLE001 - 索引创建失败同样可降级
        _warn(
            "BM25 索引创建失败，记忆检索将在运行时标记 degraded 并降级为词法召回"
            f"（{exc}）"
        )


def downgrade() -> None:
    """删除 BM25 索引。

    不删除 pg_search / vector 扩展：同一数据库内可能有其他索引或表依赖它们，
    删除扩展属于破坏性操作，交由运维显式处理。
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    with bind.begin_nested():
        bind.execute(text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))
