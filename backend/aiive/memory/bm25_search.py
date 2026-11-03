"""ParadeDB pg_search BM25 稀疏召回服务。

设计要点
--------
* 索引直接建在权威表 memory_records 上（content / canonical_key / keywords），
  不维护第二份记忆事实源：Postgres 在同一个事务里维护 bm25 索引，UPDATE /
  DELETE / ROLLBACK 与表数据一致，无需额外同步任务。
* 本服务只返回 (memory_id, 原始 BM25 分数, 排名)，调用方必须回源读取
  MemoryRecord 并重新校验 lifecycle / validity / scope / Forget 可见性。
* 可用性显式判定：扩展缺失、索引缺失、索引类型不符、SQL 失败都返回
  degraded 状态，调用方据此降级并在 MemoryRecallPack 上标记 degraded。

索引 DDL（由 Alembic 迁移创建，见 backend/alembic/versions/*_add_memory_bm25_index.py）::

    CREATE INDEX ix_memory_records_bm25 ON memory_records USING bm25 (
        id,
        (content::pdb.jieba),
        (canonical_key::pdb.literal_normalized),
        ((keywords)::text::pdb.jieba)
    );

查询 SQL::

    SELECT m.id AS memory_id, paradedb.score(m.id) AS score
    FROM memory_records m
    WHERE m.id @@@ paradedb.boolean(should => ARRAY[
            paradedb.match_disjunction('content', :query),
            paradedb.match_disjunction('canonical_key', :query),
            paradedb.match_disjunction('keywords', :query)
          ])
      AND m.lifecycle_state IN (...)
      AND m.validity_state IN (...)
      AND (<scope chain>)
    ORDER BY score DESC, m.id ASC
    LIMIT :limit
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 与 Alembic 迁移中的索引名保持一致。
BM25_INDEX_NAME = "ix_memory_records_bm25"

# 索引 DDL 的运行时副本：迁移文件冻结了同一份 DDL（迁移必须可重放，不能随
# 应用代码漂移），集成测试用本常量在真实 PostgreSQL 上重建同名索引。
BM25_INDEX_DDL = f"""
CREATE INDEX IF NOT EXISTS {BM25_INDEX_NAME} ON memory_records USING bm25 (
    id,
    (content::pdb.jieba),
    (canonical_key::pdb.literal_normalized),
    ((keywords)::text::pdb.jieba)
)
"""

# ready 之外的取值都意味着「BM25 未生效」，调用方必须降级并标记 degraded。
Bm25Status = Literal[
    "ready",
    "unsupported_dialect",
    "extension_missing",
    "index_missing",
    "index_incompatible",
    "error",
]


@dataclass(frozen=True)
class Bm25Hit:
    """单条 BM25 命中：记忆 ID、原始分数、1 基排名。"""

    memory_id: str
    score: float
    rank: int


class Bm25SearchService:
    """pg_search BM25 检索服务（只读、无缓存、按需探测可用性）。"""

    def __init__(self, db: Session, *, index_name: str = BM25_INDEX_NAME) -> None:
        self._db: Session = db
        self._index_name: str = index_name
        self._status: Bm25Status | None = None

    # ------------------------------------------------------------------
    # 可用性
    # ------------------------------------------------------------------

    @property
    def status(self) -> Bm25Status:
        """当前 BM25 可用性（进程内按 Session 缓存一次探测结果）。"""
        if self._status is None:
            self._status = self._detect_status()
        return self._status

    @property
    def available(self) -> bool:
        return self.status == "ready"

    def _detect_status(self) -> Bm25Status:
        try:
            if self._db.get_bind().dialect.name != "postgresql":
                return "unsupported_dialect"
            extension_ready = bool(self._db.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_search')")
            ).scalar())
            if not extension_ready:
                return "extension_missing"
            access_method = self._db.execute(
                text(
                    """
                    SELECT am.amname FROM pg_class c
                    JOIN pg_am am ON am.oid = c.relam
                    WHERE c.oid = to_regclass(:index_name)
                    """
                ),
                {"index_name": self._index_name},
            ).scalar()
            if access_method is None:
                return "index_missing"
            if access_method != "bm25":
                return "index_incompatible"
            return "ready"
        except SQLAlchemyError:
            logger.warning("BM25 可用性探测失败，按 degraded 处理", exc_info=True)
            return "error"

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        scope_chain: list[tuple[str, str | None]],
        lifecycle_states: list[str],
        validity_states: list[str],
        limit: int,
    ) -> list[Bm25Hit] | None:
        """执行 BM25 检索。

        Returns:
            命中列表（按 BM25 分数降序，rank 从 1 开始）；BM25 不可用或执行
            失败时返回 None —— 调用方必须据此降级，不能当作「零命中」。
        """
        q = (query or "").strip()
        if not q or limit <= 0:
            return []
        if not self.available:
            return None
        if not lifecycle_states or not validity_states:
            return []

        scope_sql, params = self._scope_params(scope_chain)
        # 用 match_disjunction（should/OR 语义）而非 parse：自然语言查询里
        # 「最近聊到的手冲咖啡」在 parse 下会退化成短语查询（要求词序相邻），
        # 中文整句几乎必然落空。match_disjunction 用索引上的 jieba tokenizer
        # 对 query 分词后做 OR 匹配，等价于「任一查询词命中」，与旧词法路由
        # 的召回语义一致，同时分数仍是真 BM25。
        statement = f"""
            SELECT m.id AS memory_id, paradedb.score(m.id) AS score
            FROM memory_records m
            WHERE m.id @@@ paradedb.boolean(should => ARRAY[
                    paradedb.match_disjunction('content', :query),
                    paradedb.match_disjunction('canonical_key', :query),
                    paradedb.match_disjunction('keywords', :query)
                  ])
              AND m.lifecycle_state IN :lifecycle_states
              AND m.validity_state IN :validity_states
              AND ({scope_sql})
            ORDER BY score DESC, m.id ASC
            LIMIT :limit
        """
        sql = text(statement).bindparams(
            bindparam("lifecycle_states", expanding=True),
            bindparam("validity_states", expanding=True),
        )
        params.update({
            "query": q,
            "limit": int(limit),
            "lifecycle_states": list(lifecycle_states),
            "validity_states": list(validity_states),
        })
        try:
            rows = self._db.execute(sql, params).all()
        except SQLAlchemyError:
            logger.warning("BM25 检索失败，本轮降级为词法召回", exc_info=True)
            self._status = "error"
            return None

        hits: list[Bm25Hit] = []
        for row in rows:
            memory_id = row[0]
            score = row[1]
            if memory_id is None or score is None:
                continue
            hits.append(Bm25Hit(
                memory_id=str(memory_id),
                score=float(score),
                rank=len(hits) + 1,
            ))
        return hits

    @staticmethod
    def _scope_params(
        scope_chain: list[tuple[str, str | None]],
    ) -> tuple[str, dict[str, Any]]:
        """把 ScopeContext.chain() 编译成 fail-closed 的 SQL 片段与参数。"""
        clauses: list[str] = []
        params: dict[str, Any] = {}
        for index, (scope_type, scope_id) in enumerate(scope_chain):
            if scope_id is None:
                clauses.append(
                    f"(m.scope_type = :st_{index} AND m.scope_id IS NULL)"
                )
            else:
                clauses.append(
                    f"(m.scope_type = :st_{index} AND m.scope_id = :si_{index})"
                )
                params[f"si_{index}"] = scope_id
            params[f"st_{index}"] = scope_type
        if not clauses:
            # 空链不可能命中任何记录：宁可返回空，也不做全表匹配。
            return "FALSE", params
        return " OR ".join(clauses), params
