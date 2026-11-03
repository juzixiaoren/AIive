"""真实 PostgreSQL + ParadeDB pg_search 的 BM25 召回集成测试。

与 test_pg_vector_rag.py 的区别：本模块验证的是**真正的 BM25 稀疏召回**
（pg_search bm25 索引 + paradedb.score），不依赖 Embedding 服务，因此
不 mock 任何检索实现，只对 Dense 路由注入一个确定性替身（避免依赖 TEI）。

前置条件（缺一即跳过）：
1. AIIVE_PG_TESTS=1；
2. 提供 AIIVE_PG_BM25_URL（已装 pg_search 的库，调用方负责隔离），或
   Docker 可用且本地存在 AIIVE_PG_BM25_IMAGE（默认 aiive-pg-bm25:pg16，
   由 deploy/pg/Dockerfile 构建：pgvector + pg_search）。

覆盖：中文/英文/混排 BM25、canonical_key 标识符匹配、Dense+BM25 双路去重与
RRF、Exact 确定性保留、类型感知 Recency、Scope 隔离、forgotten/archived、
Shield/Tombstone 过滤、索引在 UPDATE/DELETE/ROLLBACK 后的一致性、BM25 降级。
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aiive.config import settings
from aiive.db import forget_models, retention_models  # noqa: F401  注册全部表
from aiive.db.forget_models import ForgetOperation, ForgetShield, ForgetTombstone
from aiive.db.models import Base, MemoryRecord
from aiive.memory.automatic_recall import AutomaticRecallEngine
from aiive.memory.bm25_search import BM25_INDEX_DDL, BM25_INDEX_NAME, Bm25SearchService
from aiive.memory.recall_config import RecallConfig
from aiive.memory.recall_models import MemoryRecallRequest, ScopeContext
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

PG_USER = "aiive"
PG_PASSWORD = "aiive_test"
PG_DB = "aiive_test"
BM25_IMAGE = os.environ.get("AIIVE_PG_BM25_IMAGE", "aiive-pg-bm25:pg16")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _docker_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=20, check=False,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001 - 探测失败即视为不可用
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_port(host: str, port: int, timeout: float = 120.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(0.5)
    return False


@pytest.fixture(scope="module")
def bm25_pg_url():
    env_url = os.environ.get("AIIVE_PG_BM25_URL")
    if env_url:
        yield env_url
        return
    if not _docker_available():
        pytest.skip("Docker 不可用且未提供 AIIVE_PG_BM25_URL，无法验证真实 BM25")
    name = "aiive-pg-bm25-" + uuid.uuid4().hex[:8]
    port = _free_port()
    run = subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-e", f"POSTGRES_USER={PG_USER}",
            "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
            "-e", f"POSTGRES_DB={PG_DB}",
            "-p", f"127.0.0.1:{port}:5432",
            BM25_IMAGE,
        ],
        capture_output=True, text=True, timeout=180, check=False,
    )
    if run.returncode != 0:
        pytest.skip(
            "启动带 pg_search 的 PG 容器失败（先构建 deploy/pg/Dockerfile）: "
            + (run.stderr or run.stdout or "")[-400:]
        )
    try:
        if not _wait_port("127.0.0.1", port, 120):
            pytest.skip("PG 容器端口未就绪")
        yield f"postgresql+psycopg://{PG_USER}:{PG_PASSWORD}@127.0.0.1:{port}/{PG_DB}"
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, timeout=60, check=False,
        )


@pytest.fixture(scope="module")
def bm25_engine(bm25_pg_url):
    engine = create_engine(bm25_pg_url, pool_pre_ping=True)
    last_err: Exception | None = None
    for _ in range(60):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            last_err = None
            break
        except Exception as exc:  # noqa: BLE001 - 等待 PG 接受连接
            last_err = exc
            time.sleep(1)
    if last_err is not None:
        engine.dispose()
        pytest.skip("PG 连接不可用: " + str(last_err))

    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_search CASCADE"))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(BM25_INDEX_DDL))
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def bm25_sessionmaker(bm25_engine):
    return sessionmaker(bind=bm25_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _enable_bm25(monkeypatch):
    """开启 BM25、关闭向量：本模块只验证稀疏路由与融合逻辑。"""
    monkeypatch.setattr(settings, "aiive_memory_bm25_enabled", True)
    monkeypatch.setattr(settings, "aiive_memory_vector_enabled", False)
    monkeypatch.setattr(settings, "aiive_memory_bm25_top_k", 16)
    yield


@pytest.fixture(autouse=True)
def _clean_pg(bm25_engine):
    """覆盖 pg/conftest 的同名夹具：只清理本模块自己的库。"""
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with bm25_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture(autouse=True)
def _route_sessionlocal(bm25_sessionmaker):
    """覆盖 pg/conftest 的同名夹具，避免为无关模块再起一个容器。"""
    from tests import support

    original = support._real_session_local
    support._real_session_local = bm25_sessionmaker
    yield
    support._real_session_local = original


@pytest.fixture
def db(bm25_sessionmaker):
    session: Session = bm25_sessionmaker()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk(
    db: Session,
    content: str,
    *,
    memory_type: str = "knowledge",
    canonical_key: str | None = None,
    scope_type: str = "global",
    scope_id: str | None = None,
    lifecycle: str = "active",
    validity: str = "valid",
    importance: float = 0.5,
    trust: str = "semi_trusted",
    observed_at: datetime | None = None,
    keywords: list[str] | None = None,
) -> MemoryRecord:
    record = MemoryRecord(
        canonical_key=canonical_key,
        content=content,
        memory_type=memory_type,
        scope_type=scope_type,
        scope_id=scope_id,
        lifecycle_state=lifecycle,
        validity_state=validity,
        importance=importance,
        trust_level=trust,
        record_version=1,
        observed_at=observed_at,
        keywords=keywords,
    )
    db.add(record)
    db.flush()
    return record


def _engine(db: Session, *, config: RecallConfig | None = None, vector_service=None,
            bm25_service=None) -> AutomaticRecallEngine:
    return AutomaticRecallEngine(
        db, config or RecallConfig(), vector_service=vector_service,
        bm25_service=bm25_service,
    )


def _recall(db: Session, query: str, *, scope: ScopeContext | None = None,
            engine: AutomaticRecallEngine | None = None, top_k: int = 8):
    eng = engine or _engine(db)
    return eng.recall(MemoryRecallRequest(
        query=query,
        scope_context=scope or ScopeContext(),
        top_k=top_k,
        token_budget=4000,
    ))


# ---------------------------------------------------------------------------
# 1. 真 BM25：中英混排检索
# ---------------------------------------------------------------------------

def test_bm25_index_is_paradedb_bm25_scan(db: Session):
    """索引访问方法必须是 bm25，且查询计划走 ParadeDB 扫描（不是 ts_rank/ILIKE）。"""
    _mk(db, "用户偏好：每天早上喝一杯手冲咖啡，不加奶。", canonical_key="user.coffee")
    db.commit()

    conn = db.connection()
    am = conn.execute(text(
        "SELECT am.amname FROM pg_class c JOIN pg_am am ON am.oid = c.relam "
        "WHERE c.oid = to_regclass(:name)"
    ), {"name": BM25_INDEX_NAME}).scalar()
    plan = "\n".join(
        row[0] for row in conn.execute(text(
            "EXPLAIN SELECT id, paradedb.score(id) FROM memory_records "
            "WHERE id @@@ paradedb.boolean(should => ARRAY["
            "  paradedb.match_disjunction('content', '咖啡')]) "
            "ORDER BY paradedb.score(id) DESC LIMIT 5"
        ))
    )
    assert am == "bm25"
    assert "ParadeDB" in plan


def test_bm25_recalls_chinese_memory(db: Session):
    coffee = _mk(db, "用户偏好：每天早上喝一杯手冲咖啡，不加奶。")
    _mk(db, "The sky is blue and the grass is green.")
    db.commit()

    pack, traces = _recall(db, "手冲咖啡")

    assert [item.memory_id for item in pack.items] == [coffee.id]
    assert pack.items[0].route == "bm25"
    assert pack.degraded is False
    bm25_trace = [t for t in traces if t.route == "bm25" and t.memory_id == coffee.id]
    assert bm25_trace and bm25_trace[0].selected is True
    assert bm25_trace[0].route_raw_score > 0  # 真实 BM25 原始分


def test_bm25_recalls_english_memory(db: Session):
    editor = _mk(db, "User prefers dark mode in the IDE.", canonical_key="user.theme")
    _mk(db, "用户偏好：每天早上喝一杯手冲咖啡。")
    db.commit()

    pack, _ = _recall(db, "dark mode")

    assert [item.memory_id for item in pack.items] == [editor.id]
    assert pack.items[0].route == "bm25"


def test_bm25_recalls_mixed_language_memory(db: Session):
    infra = _mk(db, "生产环境使用 PostgreSQL 16 与 pgvector 扩展。", canonical_key="infra.db")
    _mk(db, "用户偏好：每天早上喝一杯手冲咖啡。")
    db.commit()

    pack, _ = _recall(db, "PostgreSQL 扩展")

    assert [item.memory_id for item in pack.items] == [infra.id]
    assert pack.items[0].route == "bm25"


def test_bm25_keywords_field_participates(db: Session):
    """keywords 也进入 BM25 索引：显式同义词仍能被召回。"""
    tea = _mk(
        db, "用户喜欢霸王茶姬的伯牙绝弦。",
        canonical_key="user.tea", keywords=["奶茶", "霸王茶姬"],
    )
    db.commit()

    pack, _ = _recall(db, "奶茶")

    assert [item.memory_id for item in pack.items] == [tea.id]


def test_bm25_canonical_key_identifier_match(db: Session):
    """canonical_key 用 literal_normalized：整串标识符可命中，非整串不命中。"""
    record = _mk(db, "编辑器主题偏好。", canonical_key="user.preference.editor_theme")
    db.commit()

    pack, traces = _recall(db, "user.preference.editor_theme")
    assert [item.memory_id for item in pack.items] == [record.id]
    routes = set(pack.items[0].route.split("|"))
    assert {"exact", "bm25"} <= routes
    assert any(
        "bm25" in t.route.split("|") and t.memory_id == record.id for t in traces
    )

    other_pack, _ = _recall(db, "user.preference.shell_theme")
    assert record.id not in [item.memory_id for item in other_pack.items]


# ---------------------------------------------------------------------------
# 2. Dense + BM25 双路：去重、RRF、Exact 保留
# ---------------------------------------------------------------------------

def test_dense_and_bm25_dedup_and_rrf(db: Session):
    """同一记忆被两路命中时只出现一次，route 记两条路由。"""
    both = _mk(db, "用户偏好：每天早上喝一杯手冲咖啡。", canonical_key="user.coffee")
    dense_only = _mk(db, "用户喜欢在夜间使用深色界面。", canonical_key="user.theme")
    db.commit()

    class StubVector:
        @staticmethod
        def search(request, *, include_sleeping, include_archived, limit):
            return [(both, 0.93), (dense_only, 0.71)]

    pack, _ = _recall(db, "手冲咖啡", engine=_engine(db, vector_service=StubVector()))

    ids = [item.memory_id for item in pack.items]
    assert ids.count(both.id) == 1
    assert both.id in ids and dense_only.id in ids
    fused = {item.memory_id: item for item in pack.items}
    assert set(fused[both.id].route.split("|")) == {"bm25", "vector"}
    assert fused[dense_only.id].route == "vector"
    # 双路命中排在单路命中之前（RRF 叠加）
    assert ids[0] == both.id


def test_exact_candidate_survives_strict_threshold(db: Session):
    """Exact 是确定性候选：即使阈值极高、BM25 相关性很低也不能被丢弃。"""
    record = _mk(db, "响应风格：简洁。", canonical_key="user.preference.response_style")
    db.commit()

    cfg = RecallConfig(recall_relevance_threshold=0.99)
    pack, _ = _recall(db, "user.preference.response_style", engine=_engine(db, config=cfg))

    assert [item.memory_id for item in pack.items] == [record.id]
    assert pack.items[0].route.split("|")[0] == "exact"


# ---------------------------------------------------------------------------
# 3. Recent Episode 迁移到统一排序
# ---------------------------------------------------------------------------

def test_recency_is_type_aware_and_intent_aware(db: Session):
    """时间敏感查询让近期情节记忆胜出；普通查询不让它压过长期偏好。"""
    now = datetime.now(UTC)
    episode = _mk(
        db, "昨天在咖啡馆聊到想试试手冲咖啡。", memory_type="episodic",
        canonical_key="event.coffee_chat", importance=0.3,
        observed_at=now - timedelta(hours=2),
    )
    preference = _mk(
        db, "用户长期偏好手冲咖啡，不加奶。", memory_type="user_profile",
        canonical_key="user.preference.coffee", importance=0.9,
        observed_at=now - timedelta(days=200),
    )
    db.commit()

    time_sensitive, _ = _recall(db, "最近聊到的手冲咖啡")
    assert time_sensitive.items[0].memory_id == episode.id

    ordinary, _ = _recall(db, "手冲咖啡")
    ordinary_ids = [item.memory_id for item in ordinary.items]
    assert preference.id in ordinary_ids
    assert ordinary_ids[0] == preference.id


# ---------------------------------------------------------------------------
# 4. Scope 隔离与生命周期
# ---------------------------------------------------------------------------

def test_bm25_respects_scope_isolation(db: Session):
    mine = _mk(db, "这个线程里用英文回复。", scope_type="thread", scope_id="t-mine")
    other = _mk(db, "另一个线程里用英文回复。", scope_type="thread", scope_id="t-other")
    db.commit()

    pack, _ = _recall(db, "英文回复", scope=ScopeContext(thread_id="t-mine"))

    ids = [item.memory_id for item in pack.items]
    assert mine.id in ids
    assert other.id not in ids


def test_forgotten_and_archived_are_not_returned(db: Session):
    active = _mk(db, "用户偏好：周末徒步带足饮水。")
    archived = _mk(db, "旧偏好：周末宅家。", lifecycle="archived", validity="expired")
    forgotten = _mk(db, "已遗忘：某段私人对话。", lifecycle="forgotten")
    db.commit()

    pack, _ = _recall(db, "周末")
    ids = [item.memory_id for item in pack.items]
    assert active.id in ids
    assert archived.id not in ids
    assert forgotten.id not in ids

    eng = _engine(db)
    with_archived, _ = eng.recall(
        MemoryRecallRequest(query="周末", scope_context=ScopeContext(), top_k=8,
                            token_budget=4000),
        include_archived=True,
    )
    archived_ids = [item.memory_id for item in with_archived.items]
    assert archived.id in archived_ids
    assert forgotten.id not in archived_ids


def test_forget_shield_blocks_bm25_hits(db: Session):
    """BM25 命中但被 Shield 屏蔽的记忆不得出现在结果里（fail-closed）。"""
    secret = _mk(db, "用户提到过某个私密项目代号。", canonical_key="secret.project")
    visible = _mk(db, "用户偏好：每天早上喝一杯手冲咖啡。")
    db.commit()

    operation = ForgetOperation(
        operation_key="op-" + uuid.uuid4().hex,
        mode="memory_only",
        selector_type="memory_ids",
        selector_hash="0" * 64,
    )
    db.add(operation)
    db.flush()
    db.add(ForgetShield(
        forget_operation_id=operation.id,
        selector_type="entity",
        target_type="memory_record",
        target_id=secret.id,
        all_user_data=False,
        cutoff_created_at=datetime.now(UTC),
        status="active",
    ))
    db.commit()

    pack, _ = _recall(db, "私密项目代号")
    ids = [item.memory_id for item in pack.items]
    assert secret.id not in ids
    assert visible.id not in ids  # 与查询无关，不应被召回

    # 未屏蔽时 BM25 确实能召回同一条内容（证明上面的空结果来自 Shield 而非索引缺失）
    db.query(ForgetShield).delete()
    db.commit()
    unshielded, _ = _recall(db, "私密项目代号")
    assert secret.id in [item.memory_id for item in unshielded.items]


def test_forget_tombstone_blocks_bm25_hits(db: Session):
    secret = _mk(db, "用户提到过某个私密项目代号。")
    db.commit()

    operation = ForgetOperation(
        operation_key="op-" + uuid.uuid4().hex,
        mode="memory_only",
        selector_type="memory_ids",
        selector_hash="1" * 64,
    )
    db.add(operation)
    db.flush()
    db.add(ForgetTombstone(
        forget_operation_id=operation.id,
        target_type="memory_record",
        target_id=secret.id,
        block_visibility=True,
        block_reingestion=True,
        content_purged=False,
        allow_audit_read=False,
    ))
    db.commit()

    pack, _ = _recall(db, "私密项目代号")
    assert secret.id not in [item.memory_id for item in pack.items]


# ---------------------------------------------------------------------------
# 5. 索引一致性：UPDATE / DELETE / ROLLBACK
# ---------------------------------------------------------------------------

def test_bm25_index_follows_update_and_scrub(db: Session):
    record = _mk(db, "用户偏好：每天早上喝一杯手冲咖啡。")
    db.commit()
    assert record.id in [i.memory_id for i in _recall(db, "手冲")[0].items]

    record.content = "用户偏好：每天早上喝一杯美式咖啡。"
    record.record_version = 2
    db.commit()
    assert record.id not in [i.memory_id for i in _recall(db, "手冲")[0].items]
    assert record.id in [i.memory_id for i in _recall(db, "美式")[0].items]

    # forget 的 scrub 路径：内容置为 [forgotten] 后旧词不可再检索
    record.content = "[forgotten]"
    record.lifecycle_state = "forgotten"
    record.record_version = 3
    db.commit()
    assert record.id not in [i.memory_id for i in _recall(db, "美式")[0].items]


def test_bm25_index_drops_committed_delete(db: Session):
    """提交后的物理删除（遗忘 purge 路径）不再被 BM25 返回。"""
    record = _mk(db, "用户偏好：每天早上喝一杯手冲咖啡。")
    db.commit()
    assert record.id in [i.memory_id for i in _recall(db, "手冲")[0].items]

    db.execute(text("DELETE FROM memory_records WHERE id = :i"), {"i": record.id})
    db.commit()

    assert record.id not in [i.memory_id for i in _recall(db, "手冲")[0].items]


def test_bm25_index_respects_transaction_rollback(db: Session):
    record = _mk(db, "用户偏好：每天早上喝一杯手冲咖啡。")
    db.commit()

    db.execute(
        text("UPDATE memory_records SET content = :c WHERE id = :i"),
        {"c": "用户偏好：每天晚上喝一杯红茶。", "i": record.id},
    )
    assert record.id not in [i.memory_id for i in _recall(db, "手冲")[0].items]
    db.rollback()
    assert record.id in [i.memory_id for i in _recall(db, "手冲")[0].items]

    db.execute(text("DELETE FROM memory_records WHERE id = :i"), {"i": record.id})
    assert record.id not in [i.memory_id for i in _recall(db, "手冲")[0].items]
    db.rollback()
    assert record.id in [i.memory_id for i in _recall(db, "手冲")[0].items]


# ---------------------------------------------------------------------------
# 6. 两路独立启停与 BM25 降级
# ---------------------------------------------------------------------------

def test_bm25_disabled_falls_back_to_lexical_without_degraded(db: Session, monkeypatch: pytest.MonkeyPatch):
    record = _mk(db, "用户偏好：每天早上喝一杯手冲咖啡。")
    db.commit()
    monkeypatch.setattr(settings, "aiive_memory_bm25_enabled", False)

    pack, _ = _recall(db, "手冲咖啡")

    assert [item.memory_id for item in pack.items] == [record.id]
    assert pack.items[0].route == "lexical"
    assert pack.degraded is False  # 显式关闭不等于降级


def test_bm25_missing_index_is_marked_degraded(db: Session):
    """索引缺失：显式 degraded + 词法兜底，不能谎称 BM25 已生效。"""
    record = _mk(db, "用户偏好：每天早上喝一杯手冲咖啡。")
    db.commit()

    service = Bm25SearchService(db, index_name="ix_memory_records_bm25_missing")
    assert service.status == "index_missing"
    assert service.search(
        "咖啡", scope_chain=[("global", None)],
        lifecycle_states=["active"], validity_states=["valid"], limit=5,
    ) is None

    pack, _ = _recall(db, "手冲咖啡", engine=_engine(db, bm25_service=service))

    assert [item.memory_id for item in pack.items] == [record.id]
    assert pack.items[0].route == "lexical"
    assert pack.degraded is True
    assert "index_missing" in pack.degraded_reason


def test_unified_retriever_auto_uses_bm25(db: Session):
    """普通聊天的 AUTO 检索链路（UnifiedRetriever → 记忆路由）确实走 BM25。"""
    from aiive.retrieval.retrieval_types import RetrievalMode, RetrievalRequest
    from aiive.retrieval.unified_retriever import UnifiedRetriever

    record = _mk(db, "用户偏好：每天早上喝一杯手冲咖啡。")
    db.commit()

    result = UnifiedRetriever(db, None, RecallConfig()).retrieve(RetrievalRequest(
        query="手冲咖啡",
        mode=RetrievalMode.AUTO,
        scope_context=ScopeContext(),
        thread_id=None,
    ))

    memory_hits = [h for h in result.hits if h.source_type == "memory_record"]
    assert any(h.source_id == record.id for h in memory_hits)
    assert result.degraded is False
    assert not any(note.startswith("memory_recall_degraded") for note in result.notes)


def test_unified_retriever_marks_bm25_degraded(db: Session):
    """索引不可用时，AUTO 链路把降级写进 notes/degraded，不谎称 BM25 生效。"""
    from aiive.retrieval.retrieval_types import RetrievalMode, RetrievalRequest
    from aiive.retrieval.unified_retriever import UnifiedRetriever

    record = _mk(db, "用户偏好：每天早上喝一杯手冲咖啡。")
    db.commit()

    # 真实改状态而不是 mock：把索引改名后 BM25 检测为 index_missing，
    # 走完整降级链路，结束时（含异常）恢复原名。
    db.execute(text(f"ALTER INDEX {BM25_INDEX_NAME} RENAME TO ix_memory_records_bm25_off"))
    db.commit()
    try:
        result = UnifiedRetriever(db, None, RecallConfig()).retrieve(RetrievalRequest(
            query="手冲咖啡",
            mode=RetrievalMode.AUTO,
            scope_context=ScopeContext(),
            thread_id=None,
        ))
    finally:
        db.rollback()
        db.execute(text(f"ALTER INDEX ix_memory_records_bm25_off RENAME TO {BM25_INDEX_NAME}"))
        db.commit()

    memory_hits = [h for h in result.hits if h.source_type == "memory_record"]
    assert any(h.source_id == record.id for h in memory_hits)  # 词法兜底仍召回
    assert result.degraded is True
    assert any(
        note.startswith("memory_recall_degraded") and "index_missing" in note
        for note in result.notes
    )


def test_vector_disabled_still_recalls_via_bm25(db: Session):
    record = _mk(db, "用户偏好：每天早上喝一杯手冲咖啡。")
    db.commit()

    pack, _ = _recall(db, "手冲咖啡")

    assert [item.memory_id for item in pack.items] == [record.id]
    assert pack.items[0].route == "bm25"
    assert pack.degraded is False



# ---------------------------------------------------------------------------
# 7. 故障隔离：BM25 SQL 失败只回滚自己的 SAVEPOINT
# ---------------------------------------------------------------------------

def test_bm25_sql_failure_rolls_back_only_its_savepoint(
    db: Session, monkeypatch: pytest.MonkeyPatch,
):
    """BM25 SQL 失败后：词法降级与 Forget 过滤仍可执行，上层事务不被回滚。

    没有 SAVEPOINT 隔离时，PostgreSQL 会把整个事务标记为 aborted，随后的
    _route_lexical 与 ForgetVisibilityService 查询都会抛 InFailedSqlTransaction。
    """
    from aiive.memory import bm25_search

    keep = _mk(db, "用户偏好：每天早上喝一杯手冲咖啡。")
    db.commit()

    # 同一事务内未提交的写入：若 BM25 失败触发上层 rollback，它会消失。
    pending = _mk(db, "用户习惯：周末沿河边跑步。")
    db.flush()

    # 真实 SQL 故障（调用不存在的函数），不是 mock 出来的异常。
    monkeypatch.setattr(
        bm25_search,
        "BM25_STATEMENT_TEMPLATE",
        "SELECT m.id AS memory_id, paradedb.no_such_function(m.id) AS score "
        "FROM memory_records m "
        "WHERE m.lifecycle_state IN :lifecycle_states "
        "AND m.validity_state IN :validity_states "
        "AND ({scope_sql}) LIMIT :limit",
    )

    pack, _ = _recall(db, "手冲咖啡")

    # 1) 失败被识别为降级，词法路由真实生效
    assert pack.degraded is True
    assert "bm25_error" in pack.degraded_reason
    assert [item.memory_id for item in pack.items] == [keep.id]
    assert pack.items[0].route == "lexical"

    # 2) 会话没有进入 aborted 状态：后续 SQL 仍可执行
    assert db.execute(text("SELECT 1")).scalar() == 1

    # 3) 上层事务没有被回滚：未提交的写入仍在，提交后依然在
    count_sql = text("SELECT count(*) FROM memory_records WHERE id = :i")
    assert db.execute(count_sql, {"i": pending.id}).scalar() == 1
    db.commit()
    assert db.execute(count_sql, {"i": pending.id}).scalar() == 1


# ---------------------------------------------------------------------------
# 8. 幂等的索引初始化 / 修复（扩展后装、索引缺失、结构不兼容）
# ---------------------------------------------------------------------------

def _drop_bm25_index(db: Session) -> None:
    db.execute(text(f"DROP INDEX IF EXISTS {BM25_INDEX_NAME}"))
    db.commit()


def test_ensure_creates_missing_index(db: Session):
    """迁移记录已完成但索引不在（被误删 / 建索引失败）：ensure 补建。"""
    from aiive.memory.bm25_search import detect_bm25_status, ensure_memory_bm25_index

    _drop_bm25_index(db)
    try:
        assert detect_bm25_status(db) == "index_missing"
        assert ensure_memory_bm25_index(db) == "ready"
        db.commit()
        assert detect_bm25_status(db) == "ready"
    finally:
        db.rollback()
        ensure_memory_bm25_index(db)
        db.commit()


def test_ensure_is_idempotent(db: Session):
    """索引结构已正确时 ensure 只做只读检查，不重建（索引 OID 不变）。"""
    from aiive.memory.bm25_search import ensure_memory_bm25_index

    oid_sql = text("SELECT to_regclass(:name)::oid")

    assert ensure_memory_bm25_index(db) == "ready"
    db.commit()
    oid_before = db.execute(oid_sql, {"name": BM25_INDEX_NAME}).scalar()

    assert ensure_memory_bm25_index(db) == "ready"
    db.commit()
    oid_after = db.execute(oid_sql, {"name": BM25_INDEX_NAME}).scalar()

    assert oid_before is not None
    assert oid_before == oid_after


def test_ensure_rebuilds_incompatible_index(db: Session):
    """同名索引是 btree（结构不兼容）：ensure 重建为 bm25 索引。"""
    from aiive.memory.bm25_search import detect_bm25_status, ensure_memory_bm25_index

    _drop_bm25_index(db)
    db.execute(text(f"CREATE INDEX {BM25_INDEX_NAME} ON memory_records (content)"))
    db.commit()
    try:
        assert detect_bm25_status(db) == "index_incompatible"
        # 显式关闭重建时只报告，不删除索引
        assert ensure_memory_bm25_index(db, rebuild_incompatible=False) == "index_incompatible"
        # 默认重建
        assert ensure_memory_bm25_index(db) == "ready"
        db.commit()
        assert detect_bm25_status(db) == "ready"
    finally:
        db.rollback()
        _drop_bm25_index(db)
        ensure_memory_bm25_index(db)
        db.commit()


def test_ensure_installs_extension_in_late_enabled_database(bm25_engine):
    """扩展后装：迁移当时没有 pg_search，之后装好也能补建索引（不重建数据库）。"""
    from aiive.memory.bm25_search import detect_bm25_status, ensure_memory_bm25_index

    name = "aiive_bm25_late_" + uuid.uuid4().hex[:8]
    with bm25_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))

    fresh = create_engine(bm25_engine.url.set(database=name))
    try:
        # 新库只有 pgvector：与「迁移已记录完成、pg_search 尚未安装」的状态一致
        with fresh.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(fresh)

        with Session(fresh) as session:
            assert detect_bm25_status(session) == "extension_missing"
            assert ensure_memory_bm25_index(session) == "ready"
            session.commit()
            assert detect_bm25_status(session) == "ready"
            access_method = session.execute(text(
                "SELECT am.amname FROM pg_class c JOIN pg_am am ON am.oid = c.relam "
                "WHERE c.oid = to_regclass(:name)"
            ), {"name": BM25_INDEX_NAME}).scalar()
            assert access_method == "bm25"
    finally:
        fresh.dispose()
        with bm25_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name}"))
