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
* 故障隔离：所有可恢复语句（可用性探测、BM25 查询、索引维护）都跑在独立的
  SAVEPOINT 里。语句失败只回滚到该 SAVEPOINT，PostgreSQL 事务不会进入
  aborted 状态，因此后续的词法降级召回与 ForgetVisibilityService 过滤仍能
  正常执行，也绝不会回滚调用方的上层业务事务。

索引 DDL（由 Alembic 迁移创建，见 backend/alembic/versions/*_add_memory_bm25_index.py）::

    CREATE INDEX ix_memory_records_bm25 ON memory_records USING bm25 (
        id,
        (content::pdb.jieba),
        (canonical_key::pdb.literal_normalized),
        ((keywords)::text::pdb.jieba)
    );

迁移只在升级那一刻执行一次；扩展是后装的、索引被误删或结构被改坏时迁移不会
重跑。这类情况由 ensure_memory_bm25_index() 兜底：应用启动时自动调用一次，
也可通过 python -m aiive.memory.bm25_maintenance ensure 显式修复。

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
# 应用代码漂移），集成测试与运行时补建都用本常量重建同名索引。
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

# 索引必须满足的结构特征。pg_get_indexdef 会给表达式加括号（例如
# ((content)::pdb.jieba)），因此按 tokenizer 与列名判定，不比对括号形式。
_REQUIRED_DEFINITION_FRAGMENTS = (
    "using bm25",
    "memory_records",
    "content",
    "pdb.jieba",
    "canonical_key",
    "pdb.literal_normalized",
    "keywords",
)

_EXTENSION_EXISTS_SQL = (
    "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_search')"
)
_INDEX_ACCESS_METHOD_SQL = """
    SELECT am.amname FROM pg_class c
    JOIN pg_am am ON am.oid = c.relam
    WHERE c.oid = to_regclass(:index_name)
"""
_INDEX_DEFINITION_SQL = "SELECT pg_get_indexdef(to_regclass(:index_name))"

# 查询语句模板：抽成模块常量便于测试注入故障 SQL，验证 SAVEPOINT 隔离。
BM25_STATEMENT_TEMPLATE = """
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

# 每种不可用状态的修复指引（启动日志与维护命令共用）。
REMEDIATION_HINTS: dict[str, str] = {
    "extension_missing": (
        "pg_search 扩展不可用：确认镜像由 deploy/pg/Dockerfile 构建，且 "
        "shared_preload_libraries 包含 pg_search（旧数据卷由 "
        "deploy/pg/ensure-preload.sh 在容器启动时自动补齐，补齐后需重启 PostgreSQL）。"
    ),
    "index_missing": (
        "索引缺失：执行 python -m aiive.memory.bm25_maintenance ensure 补建。"
    ),
    "index_incompatible": (
        "索引结构不兼容（非 bm25 访问方法或缺少被索引表达式）：执行同一命令重建。"
    ),
    "error": "探测过程出现 SQL 错误：检查数据库连接与日志。",
    "unsupported_dialect": "当前数据库不是 PostgreSQL，BM25 不可用（召回降级为词法）。",
}


def remediation_hint(status: Bm25Status) -> str:
    """返回某个不可用状态对应的修复指引；ready 时返回空串。"""
    return REMEDIATION_HINTS.get(status, "")


@dataclass(frozen=True)
class Bm25Hit:
    """单条 BM25 命中：记忆 ID、原始分数、1 基排名。"""

    memory_id: str
    score: float
    rank: int


# ---------------------------------------------------------------------------
# 探测与维护（模块级函数：检索服务与维护命令共用同一份判定逻辑）
# ---------------------------------------------------------------------------


def _is_postgres(db: Session) -> bool:
    try:
        return db.get_bind().dialect.name == "postgresql"
    except SQLAlchemyError:
        return False


def _scalar_in_savepoint(
    db: Session, sql: str, params: dict[str, Any] | None = None,
) -> Any:
    """在独立 SAVEPOINT 内执行一条只读语句。

    失败时 SAVEPOINT 被回滚、异常继续上抛，但上层事务不会被标记为 aborted。
    """
    with db.begin_nested():
        return db.execute(text(sql), params or {}).scalar()


def _extension_installed(db: Session) -> bool:
    return bool(_scalar_in_savepoint(db, _EXTENSION_EXISTS_SQL))


def _index_definition(db: Session, index_name: str = BM25_INDEX_NAME) -> str | None:
    """返回索引定义文本；索引不存在时返回 None。"""
    definition = _scalar_in_savepoint(
        db, _INDEX_DEFINITION_SQL, {"index_name": index_name},
    )
    return None if definition is None else str(definition)


def _index_is_compatible(definition: str) -> bool:
    """索引是否为本服务期望的 bm25 索引（访问方法 + 三个被索引表达式）。"""
    normalized = " ".join(definition.lower().split())
    return all(fragment in normalized for fragment in _REQUIRED_DEFINITION_FRAGMENTS)


def detect_bm25_status(
    db: Session, index_name: str = BM25_INDEX_NAME,
) -> Bm25Status:
    """判定 BM25 当前是否可用（只读，异常一律归为 error）。"""
    try:
        if not _is_postgres(db):
            return "unsupported_dialect"
        if not _extension_installed(db):
            return "extension_missing"
        access_method = _scalar_in_savepoint(
            db, _INDEX_ACCESS_METHOD_SQL, {"index_name": index_name},
        )
        if access_method is None:
            return "index_missing"
        if access_method != "bm25":
            return "index_incompatible"
        definition = _index_definition(db, index_name)
        if definition is None:
            return "index_missing"
        if not _index_is_compatible(definition):
            return "index_incompatible"
        return "ready"
    except SQLAlchemyError:
        logger.warning("BM25 可用性探测失败，按 degraded 处理", exc_info=True)
        return "error"


def _create_index(db: Session) -> bool:
    try:
        with db.begin_nested():
            db.execute(text(BM25_INDEX_DDL))
        return True
    except SQLAlchemyError:
        logger.warning("BM25 索引创建失败（BM25 保持不可用，召回降级为词法）", exc_info=True)
        return False


def _rebuild_index(db: Session) -> bool:
    """删除结构不兼容的旧索引并重建（不触碰表数据）。"""
    try:
        with db.begin_nested():
            db.execute(text(f"DROP INDEX IF EXISTS {BM25_INDEX_NAME}"))
    except SQLAlchemyError:
        logger.warning("旧 BM25 索引删除失败", exc_info=True)
        return False
    return _create_index(db)


def ensure_memory_bm25_index(
    db: Session, *, rebuild_incompatible: bool = True,
) -> Bm25Status:
    """幂等地保证 BM25 索引存在且结构正确，返回修复后的状态。

    覆盖三种「迁移已记录完成、索引却不可用」的场景：

    * 扩展后装：迁移执行时 pg_search 尚未安装（镜像未带扩展 / 未预加载），
      之后运维装好了扩展 —— 这里补建索引；
    * 索引缺失：索引被误删、或迁移当时建索引失败 —— 这里补建；
    * 索引结构不兼容：同名索引不是 bm25 访问方法、或缺少被索引表达式 ——
      rebuild_incompatible=True 时删除重建，否则原样返回 index_incompatible。

    幂等：结构已正确时只做只读检查，不产生任何 DDL；所有 DDL 都在独立
    SAVEPOINT 内执行，失败不会污染调用方事务，也不会删除或重建数据库。
    """
    if not _is_postgres(db):
        return "unsupported_dialect"
    if not _extension_installed(db):
        try:
            with db.begin_nested():
                db.execute(text("CREATE EXTENSION IF NOT EXISTS pg_search CASCADE"))
        except SQLAlchemyError:
            logger.warning(
                "pg_search 扩展安装失败：镜像未包含扩展，或 shared_preload_libraries "
                "未包含 pg_search（需重启 PostgreSQL）",
                exc_info=True,
            )
            return "extension_missing"

    definition = _index_definition(db)
    if definition is None:
        if not _create_index(db):
            return detect_bm25_status(db)
    elif not _index_is_compatible(definition):
        if not rebuild_incompatible:
            logger.warning("BM25 索引 %s 结构不兼容，未按参数重建", BM25_INDEX_NAME)
            return "index_incompatible"
        logger.warning(
            "BM25 索引 %s 结构不兼容（非 bm25 访问方法或缺少被索引表达式），重建索引",
            BM25_INDEX_NAME,
        )
        if not _rebuild_index(db):
            return detect_bm25_status(db)
    return detect_bm25_status(db)


def bm25_diagnostics(db: Session) -> dict[str, str]:
    """采集 BM25 运行环境诊断信息（启动日志与维护命令使用）。"""
    info: dict[str, str] = {}
    try:
        info["dialect"] = str(db.get_bind().dialect.name)
    except SQLAlchemyError:
        info["dialect"] = "unknown"
    if info["dialect"] != "postgresql":
        info["status"] = "unsupported_dialect"
        return info
    try:
        info["shared_preload_libraries"] = str(
            _scalar_in_savepoint(db, "SHOW shared_preload_libraries") or "",
        )
    except SQLAlchemyError:
        info["shared_preload_libraries"] = "unknown"
    try:
        info["extension_installed"] = "yes" if _extension_installed(db) else "no"
    except SQLAlchemyError:
        info["extension_installed"] = "unknown"
    try:
        info["index_definition"] = _index_definition(db) or "(missing)"
    except SQLAlchemyError:
        info["index_definition"] = "(unknown)"
    info["status"] = detect_bm25_status(db)
    return info


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
            self._status = detect_bm25_status(self._db, self._index_name)
        return self._status

    @property
    def available(self) -> bool:
        return self.status == "ready"

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

        故障隔离：查询在独立 SAVEPOINT 内执行。SQL 失败时只回滚该 SAVEPOINT，
        会话不会被 PostgreSQL 标记为 aborted，调用方随后执行的词法降级召回与
        Forget 可见性过滤仍然可用，上层业务事务也不会被回滚。
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
        statement = BM25_STATEMENT_TEMPLATE.format(scope_sql=scope_sql)
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
            with self._db.begin_nested():
                rows = self._db.execute(sql, params).all()
        except SQLAlchemyError:
            logger.warning(
                "BM25 检索失败，本轮降级为词法召回（SAVEPOINT 已回滚，上层事务不受影响）",
                exc_info=True,
            )
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
