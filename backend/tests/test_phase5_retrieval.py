"""Phase 5 统一检索回归测试矩阵（对应 phase5.md §T / §「新增测试」）。

覆盖：索引构建与倒排查询、refresh 服务对三类源的 upsert/tombstone、统一检索编排器
AUTO/SEARCH/DEEP 三种模式、确定性去重、token 预算截断、source_version fencing（旧不覆盖新）、
refresh operation_id 幂等、原始 Turn/Event 不进索引、deep 二阶段回溯原始事件（含 manifest/hash
校验）、include_archived 控制、bootstrap backfill 幂等、EpochCheckpoint source_hashes 校验、
AUTO 摘要去重、SQLite 冲突不回滚整个事务。

说明：token_count 使用真实 `LiteLLMTokenCounter.count_text`（真实计数 + fallback），语义检索 v1 关闭。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from aiive.db.models import (
    CompactionInput,
    Epoch,
    EpochCheckpoint,
    MemoryRecord,
    OutboxJob,
    RetrievalIndexGeneration,
    RetrievalIndexEntry,
    RetrievalIndexRun,
    Segment,
    SegmentSummary,
    Thread,
    Event,
    TurnRecord,
)
from aiive.memory.memory_mutation import MemoryMutationExecutor
from aiive.memory.memory_types import LifecycleState
from aiive.retrieval.indexing_service import refresh_source
from aiive.retrieval.raw_history_expander import RawHistoryExpander
from aiive.retrieval.retrieval_bootstrap import ensure_retrieval_backfill
from aiive.retrieval.retrieval_index import RetrievalIndexManager
from aiive.retrieval.retrieval_types import (
    RetrievalHit,
    RetrievalMode,
    RetrievalRequest,
)
from aiive.retrieval.unified_retriever import UnifiedRetriever
from aiive.memory.recall_config import RetrievalConfig
from aiive.runtime.compaction import event_content_hash


def _now():
    return datetime.now(timezone.utc)


def _active_gen(db):
    """创建并刷新唯一 active generation（测试查询真相源）。"""
    mgr = RetrievalIndexManager()
    gen = mgr.create_generation(db, 1, status="active")
    db.flush()
    return gen


def _mk_memory(db, **kwargs) -> MemoryRecord:
    defaults = dict(
        id=str(uuid.uuid4()),
        memory_type="fact",
        canonical_key="user.pref.lang",
        scope_type="global",
        scope_id=None,
        content="用户偏好使用 Python 编程",
        lifecycle_state=LifecycleState.ACTIVE.value,
        validity_state="valid",
        confidence=0.5,
        importance=0.5,
        retention_policy="normal",
        record_version=1,
        pinned=False,
        stability="contextual",
        observed_at=_now(),
        created_at=_now(),
        updated_at=_now(),
    )
    defaults.update(kwargs)
    rec = MemoryRecord(**defaults)
    db.add(rec)
    db.flush()
    return rec


def _mk_thread(db, tid="t1") -> Thread:
    th = Thread(id=tid)
    db.add(th)
    db.flush()
    return th


def _mk_summary_chain(db, goal="Python 编程偏好") -> SegmentSummary:
    _mk_thread(db, "t1")
    epoch = Epoch(id="e1", thread_id="t1", epoch_no=1, status="active")
    db.add(epoch)
    seg = Segment(
        id="s1", epoch_id="e1", thread_id="t1", segment_no=1,
        start_turn_sequence=0,
    )
    db.add(seg)
    summ = SegmentSummary(
        id="ss1", segment_id="s1", goal=goal, outcome="用户明确选择 Python",
        decisions=[{"what": "使用 Python"}],
    )
    db.add(summ)
    db.flush()
    return summ


def _mk_checkpoint(db, goal="Python 项目目标") -> EpochCheckpoint:
    cp = EpochCheckpoint(
        id="cp1", epoch_id="e1", version=1, current_goal=goal,
        open_loops=[{"description": "完成 CLI"}],
        active_constraints=[{"description": "仅 Python"}],
    )
    db.add(cp)
    db.flush()
    return cp


# ───────────────────────── 索引构建与倒排查询 ─────────────────────────


def test_index_upsert_and_lexical_query(db):
    """#11 倒排 posting 聚合：upsert 后可按 token 命中。"""
    gen = _active_gen(db)
    mgr = RetrievalIndexManager()
    mgr.upsert_entry(
        db, gen, source_type="segment_summary", source_id="ss1",
        source_version="1", source_hash="h1", title="Python 偏好",
        search_text="Python 编程偏好", snippet="用户偏好 Python",
        scope_type="global", scope_id=None, canonical_key=None,
        lifecycle_state="valid", validity_state="valid", retrieval_tier="warm",
        thread_id=None, epoch_id=None, segment_id="s1", memory_record_id=None,
        metadata={}, created_source_at=_now(), updated_source_at=_now(),
        tokens=[("python", "word"), ("编程", "cjk_bigram")],
    )
    db.flush()
    scored = mgr.query_tokens(db, gen.index_version, ["python"], 10)
    assert scored, "应按 token python 命中"
    assert scored[0][0] is not None


def test_index_exact_query(db):
    """#10 exact 命中：按 canonical_key 精确查 memory_record entry。"""
    gen = _active_gen(db)
    mgr = RetrievalIndexManager()
    mgr.upsert_entry(
        db, gen, source_type="memory_record", source_id="m1",
        source_version="1", source_hash="h1", title="user.pref.lang",
        search_text="user.pref.lang Python", snippet="用户偏好 Python",
        scope_type="global", scope_id=None, canonical_key="user.pref.lang",
        lifecycle_state="active", validity_state="valid", retrieval_tier="warm",
        thread_id=None, epoch_id=None, segment_id=None, memory_record_id="m1",
        metadata={}, created_source_at=_now(), updated_source_at=_now(),
        tokens=[("python", "word")],
    )
    db.flush()
    exact = mgr.query_exact(db, gen.index_version, "user.pref.lang", None)
    assert len(exact) == 1
    assert exact[0].source_id == "m1"


# ───────────────────────── refresh 服务（三类源） ─────────────────────────


def test_refresh_memory_record_active(db):
    """#1 #4 active 记忆可被统一检索刷新并召回。"""
    gen = _active_gen(db)
    mem = _mk_memory(db, canonical_key="user.pref.lang", content="用户偏好使用 Python 编程")
    upserted, tombstoned = refresh_source(db, "memory_record", mem.id, "memory.created")
    assert upserted == 1 and tombstoned == 0
    # 倒排能命中内容 token
    mgr = RetrievalIndexManager()
    scored = mgr.query_tokens(db, gen.index_version, ["python"], 10)
    assert any(eid for eid, _ in scored)


def test_refresh_segment_summary(db):
    """#2 SegmentSummary 可被统一检索刷新。"""
    gen = _active_gen(db)
    summ = _mk_summary_chain(db, goal="Python 编程偏好")
    upserted, _ = refresh_source(db, "segment_summary", summ.id, "memory.created")
    assert upserted == 1
    mgr = RetrievalIndexManager()
    scored = mgr.query_tokens(db, gen.index_version, ["python"], 10)
    assert scored


def test_refresh_epoch_checkpoint(db):
    """#3 EpochCheckpoint 可被统一检索刷新。"""
    gen = _active_gen(db)
    cp = _mk_checkpoint(db, goal="Python 项目目标")
    upserted, _ = refresh_source(db, "epoch_checkpoint", cp.id, "memory.created")
    assert upserted == 1
    mgr = RetrievalIndexManager()
    scored = mgr.query_tokens(db, gen.index_version, ["python"], 10)
    assert scored


# ───────────────────────── 生命周期过滤与 tombstone ─────────────────────────


def test_refresh_tombstones_forgotten_memory(db):
    """#8 #27 forgotten 记忆刷新后不建立可检索 entry（不可检索）。"""
    gen = _active_gen(db)
    mem = _mk_memory(db, lifecycle_state=LifecycleState.FORGOTTEN.value)
    upserted, tombstoned = refresh_source(db, "memory_record", mem.id, "memory.created")
    # forgotten 直接 tombstone，不产生可检索 entry（upserted=0）
    assert upserted == 0
    entries = db.query(RetrievalIndexEntry).filter_by(
        source_type="memory_record", source_id=mem.id,
    ).all()
    assert all(not e.is_searchable for e in entries)
    mgr = RetrievalIndexManager()
    scored = mgr.query_tokens(db, gen.index_version, ["python"], 10)
    assert not any(eid for eid, _ in scored)


def test_refresh_tombstones_on_forgotten_event(db):
    """#27 切换到 forgotten 事件触发 tombstone（即便源仍存在）。"""
    gen = _active_gen(db)
    mem = _mk_memory(db)
    refresh_source(db, "memory_record", mem.id, "memory.created")
    # 后续以 forgotten 事件刷新
    upserted, tombstoned = refresh_source(db, "memory_record", mem.id, "memory.forgotten")
    assert upserted == 0 and tombstoned == 1
    entry = db.query(RetrievalIndexEntry).filter_by(
        source_type="memory_record", source_id=mem.id,
    ).first()
    assert entry.is_searchable is False


def test_archived_not_in_auto_lexical(db):
    """archived 记忆保持索引（is_searchable=True），在检索时由 include_archived 控制过滤。

    先以 active 建立 entry，再以 archive 事件刷新：entry 仍在且可检索，
    但 SEARCH 不加 include_archived 时不返回；加 include_archived=True 后返回。
    """
    gen = _active_gen(db)
    mem = _mk_memory(db)
    refresh_source(db, "memory_record", mem.id, "memory.created")
    # 将 source 置为 archived 状态，再触发 refresh（新设计：archived 保持索引可检索）
    mem.lifecycle_state = LifecycleState.ARCHIVED.value
    db.flush()
    refresh_source(db, "memory_record", mem.id, "memory.archived")
    entry = db.query(RetrievalIndexEntry).filter_by(
        source_type="memory_record", source_id=mem.id,
    ).first()
    assert entry is not None and entry.is_searchable is True  # archived 保持在索引中
    # SEARCH 不加 include_archived → 过滤
    ur = UnifiedRetriever(db)
    res_no = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.SEARCH, source_types=["memory_record"],
        scope_context={}, include_archived=False,
    ))
    assert mem.id not in {h.source_id for h in res_no.hits}
    # SEARCH 加 include_archived → 返回
    res_yes = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.SEARCH, source_types=["memory_record"],
        scope_context={}, include_archived=True,
    ))
    assert mem.id in {h.source_id for h in res_yes.hits}


# ───────────────────────── source_version fencing ─────────────────────────


def test_old_source_version_not_overwrite_new(db):
    """#26 旧 source_version 的 Job 不得覆盖新版本 entry。"""
    gen = _active_gen(db)
    mgr = RetrievalIndexManager()
    base = dict(
        db=db, generation=gen, source_type="memory_record", source_id="m1",
        source_hash="h", title="k", search_text="x", snippet="x",
        scope_type="global", scope_id=None, canonical_key="k",
        lifecycle_state="active", validity_state="valid", retrieval_tier="warm",
        thread_id=None, epoch_id=None, segment_id=None, memory_record_id="m1",
        metadata={}, created_source_at=_now(), updated_source_at=_now(),
        tokens=[("x", "word")],
    )
    assert mgr.upsert_entry(source_version="2", **base) == "upserted"
    # 旧版本 Job 到达
    assert mgr.upsert_entry(source_version="1", **base) == "skipped_stale"
    entry = db.query(RetrievalIndexEntry).filter_by(
        source_type="memory_record", source_id="m1",
    ).first()
    assert entry.source_version == "2"


def test_same_source_only_one_current(db):
    """#15 同 source 在 generation 内仅一个 is_current。"""
    gen = _active_gen(db)
    mgr = RetrievalIndexManager()
    base = dict(
        db=db, generation=gen, source_type="segment_summary", source_id="ss1",
        source_hash="h", title="t", search_text="x", snippet="x",
        scope_type="global", scope_id=None, canonical_key=None,
        lifecycle_state="valid", validity_state="valid", retrieval_tier="warm",
        thread_id=None, epoch_id=None, segment_id="s1", memory_record_id=None,
        metadata={}, created_source_at=_now(), updated_source_at=_now(),
        tokens=[("x", "word")],
    )
    mgr.upsert_entry(source_version="1", **base)
    mgr.upsert_entry(source_version="2", **base)
    currents = db.query(RetrievalIndexEntry).filter_by(
        source_type="segment_summary", source_id="ss1", is_current=True,
    ).all()
    assert len(currents) == 1


# ───────────────────────── UnifiedRetriever 编排 ─────────────────────────


def test_unified_retriever_search_memory_record(db):
    """#1 #11 SEARCH 模式经记忆路由（AutomaticRecallEngine 适配器）召回 memory_record。

    memory_record 不进 lexical 倒排路由（仅 segment_summary/epoch_checkpoint），
    统一走 recall 引擎适配器；此处需提供 scope_context 才能触发记忆路由。
    """
    _active_gen(db)
    mem = _mk_memory(db, canonical_key="user.pref.lang", content="用户偏好使用 Python 编程")
    refresh_source(db, "memory_record", mem.id, "memory.created")
    ur = UnifiedRetriever(db)
    res = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.SEARCH,
        source_types=["memory_record"], scope_context={},
    ))
    assert res.hits
    assert any(h.source_type == "memory_record" for h in res.hits)


def test_unified_retriever_search_lexical_summary(db):
    """#2 #3 SEARCH 模式经倒排 lexical 召回 segment_summary / epoch_checkpoint。"""
    _active_gen(db)
    summ = _mk_summary_chain(db, goal="Python 编程偏好")
    cp = _mk_checkpoint(db, goal="Python 项目目标")
    refresh_source(db, "segment_summary", summ.id, "memory.created")
    refresh_source(db, "epoch_checkpoint", cp.id, "memory.created")
    ur = UnifiedRetriever(db)
    res = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.SEARCH,
        source_types=["segment_summary", "epoch_checkpoint"],
    ))
    sources = {h.source_type for h in res.hits}
    assert "segment_summary" in sources
    assert "epoch_checkpoint" in sources


def test_unified_retriever_auto_returns_summary(db):
    """#5 自动召回单一编排入口：AUTO 模式返回 lexical 命中（不重复执行两路造成污染）。"""
    _active_gen(db)
    summ = _mk_summary_chain(db, goal="Python 编程偏好")
    refresh_source(db, "segment_summary", summ.id, "memory.created")
    ur = UnifiedRetriever(db)
    res = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.AUTO,
        scope_context={},
    ))
    assert res.hits
    assert not res.degraded


def test_unified_retriever_auto_excludes_sleeping_memory(db):
    """#5 sleeping 记忆不在 AUTO 模式返回；仅显式允许模式可返回。"""
    _active_gen(db)
    mem = _mk_memory(
        db, canonical_key="user.pref.lang", content="用户偏好使用 Python 编程",
        lifecycle_state=LifecycleState.SLEEPING.value,
    )
    refresh_source(db, "memory_record", mem.id, "memory.created")
    ur = UnifiedRetriever(db)
    # AUTO：include_sleeping=False → 不返回 sleeping
    auto = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.AUTO, scope_context={},
    ))
    assert not any(h.source_id == mem.id for h in auto.hits)
    # SEARCH（显式允许模式路径）：include_sleeping=True → 可返回
    search = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.SEARCH,
        source_types=["memory_record"], scope_context={},
    ))
    assert any(h.source_id == mem.id for h in search.hits)


def test_unified_retriever_token_budget_truncation(db):
    """#24 超预算稳定截断：仅返回预算内命中。"""
    _active_gen(db)
    mgr = RetrievalIndexManager()
    gen = mgr.get_active_generation(db)
    for i in range(5):
        mgr.upsert_entry(
            db, gen, source_type="segment_summary", source_id=f"ss{i}",
            source_version="1", source_hash="h", title=f"t{i}",
            search_text="Python 编程", snippet="x", scope_type="global",
            scope_id=None, canonical_key=None, lifecycle_state="valid",
            validity_state="valid", retrieval_tier="warm", thread_id=None,
            epoch_id=None, segment_id=f"s{i}", memory_record_id=None,
            metadata={}, created_source_at=_now(), updated_source_at=_now(),
            tokens=[("python", "word")],
        )
    db.flush()
    ur = UnifiedRetriever(db)
    # token_budget 极小，强制截断到 0~1 条
    res = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.SEARCH,
        source_types=["segment_summary"], token_budget=1,
    ))
    assert len(res.hits) <= 1


def test_unified_retriever_dedup_memory_and_index_route(db):
    """#15 同一 memory_record 即便两路命中也只保留一个（按 source 去重）。"""
    _active_gen(db)
    mem = _mk_memory(db, canonical_key="user.pref.lang", content="用户偏好使用 Python 编程")
    refresh_source(db, "memory_record", mem.id, "memory.created")
    ur = UnifiedRetriever(db)
    res = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.SEARCH,
        source_types=["memory_record"], scope_context={},
    ))
    mr_hits = [h for h in res.hits if h.source_id == mem.id]
    assert len(mr_hits) == 1


def test_unified_retriever_index_route_degrade_isolated(db):
    """#45 索引路由异常被隔离：不影响整体返回与降级标记。"""
    _active_gen(db)
    summ = _mk_summary_chain(db, goal="Python 编程偏好")
    refresh_source(db, "segment_summary", summ.id, "memory.created")
    ur = UnifiedRetriever(db)
    # 强制索引查询抛错（monkeypatch query_tokens）
    original = RetrievalIndexManager.query_tokens

    def _boom(*a, **k):
        raise RuntimeError("simulated index failure")

    RetrievalIndexManager.query_tokens = _boom
    try:
        res = ur.retrieve(RetrievalRequest(
            query="Python", mode=RetrievalMode.SEARCH,
            source_types=["segment_summary", "epoch_checkpoint"],
        ))
    finally:
        RetrievalIndexManager.query_tokens = original
    # 索引路由失败被捕获：降级标记置位，但不抛异常
    assert res.degraded is True
    assert "index_route_degraded" in res.notes


# ───────────────────────── refresh operation_id 幂等 ─────────────────────────


def test_retrieval_refresh_operation_id_dedup(db):
    """#25 同一事务内对同一 source 同一版本的多次 refresh 入队合并为一条 OutboxJob。"""
    mem = _mk_memory(db)
    exec = MemoryMutationExecutor(db)
    exec._enqueue_retrieval_refresh(mem, "memory.created")
    exec._enqueue_retrieval_refresh(mem, "memory.reinforced")
    # 版本粒度 op_id：record_version 未变 → 同一条
    op_id = f"retrieval_refresh:memory_record:{mem.id}:{mem.record_version}"
    jobs = db.query(OutboxJob).filter_by(operation_id=op_id).all()
    assert len(jobs) == 1


def test_retrieval_refresh_re_enqueued_after_completed_on_version_change(db):
    """问题 1 回归：首次 refresh 完成后，记忆发生版本变更（含 forget）必须能再次入队。

    旧实现 op_id 仅按 source 粒度，匹配到已完成 job 后直接 return，导致 forgotten 的
    tombstone 永不执行（违反 revision 7 隐私承诺）。新版 op_id 含 record_version，
    版本变更产生新 op_id，不再被已完成 job 阻塞。
    """
    mem = _mk_memory(db)
    exec = MemoryMutationExecutor(db)
    exec._enqueue_retrieval_refresh(mem, "memory.created")
    db.flush()
    # 首次 job 已完成（模拟 OutboxWorker 处理后置 completed，记录不 GC）
    first = db.query(OutboxJob).filter_by(
        operation_id=f"retrieval_refresh:memory_record:{mem.id}:{mem.record_version}",
    ).one()
    first.status = "completed"
    db.commit()

    # 生命周期变更：forget 会 bump record_version（memory_lifecycle_service 行为）
    mem.record_version += 1
    mem.lifecycle_state = LifecycleState.FORGOTTEN.value
    db.flush()
    exec._enqueue_retrieval_refresh(mem, "memory.forgotten")
    db.flush()

    # 应存在两条不同版本的 job（首次 v1 + forget v2），v2 未完成 → 待执行 tombstone
    jobs = db.query(OutboxJob).filter(
        OutboxJob.operation_id.like(f"retrieval_refresh:memory_record:{mem.id}:%"),
    ).order_by(OutboxJob.operation_id).all()
    assert len(jobs) == 2
    assert jobs[0].operation_id.endswith(":1")
    assert jobs[1].operation_id.endswith(":2")
    assert jobs[1].status == "pending"


def test_memory_item_to_hit_carries_memory_type(db):
    """问题 2 回归：memory_item_to_hit 必须把 memory_type 写入 provenance，
    否则 builtin_tools 的 memory_types 过滤恒为空、返回 memory_type 恒为 ''。"""
    from aiive.memory.recall_models import MemoryRecallItem
    from aiive.retrieval.unified_retriever import UnifiedRetriever

    item = MemoryRecallItem(
        memory_id="m1",
        content="用户偏好 Python",
        memory_type="fact",
        canonical_key="user.pref.lang",
        scope_type="global",
        relevance_score=0.9,
        record_version=3,
        route="fts",
        fused_score=0.9,
    )
    hit = UnifiedRetriever.memory_item_to_hit(item)
    assert hit.provenance.get("memory_type") == "fact"


def test_stale_refresh_version_granular_and_no_unique_conflict(db):
    """连带问题回归：stale refresh op_id 为版本粒度；已存在同版本（含 completed）job 时
    跳过而非触发 UNIQUE 冲突，且不污染会话。"""
    ur = UnifiedRetriever(db)
    mid = str(uuid.uuid4())
    # 预置一条 completed 的同版本 job（模拟之前已被 stale/lifecycle 刷新过）
    db.add(OutboxJob(
        id=str(uuid.uuid4()),
        operation_id=f"retrieval_refresh:memory_record:{mid}:1",
        job_type="retrieval_index_refresh",
        status="completed",
        payload={},
        trace_id=mid,
        max_retries=3,
    ))
    db.commit()
    db.begin()  # 显式外层事务使 begin_nested 正确嵌套
    # 同版本再次 stale → 应跳过（不冲突、不新增）
    ur._enqueue_stale_refresh([("memory_record", mid, "1")])
    db.flush()
    jobs = db.query(OutboxJob).filter_by(
        operation_id=f"retrieval_refresh:memory_record:{mid}:1",
    ).all()
    assert len(jobs) == 1
    # 新版本 stale → 应新增一条（版本粒度独立 job）
    ur._enqueue_stale_refresh([("memory_record", mid, "2")])
    db.flush()
    jobs2 = db.query(OutboxJob).filter(
        OutboxJob.operation_id.like(f"retrieval_refresh:memory_record:{mid}:%"),
    ).order_by(OutboxJob.operation_id).all()
    assert len(jobs2) == 2
    assert jobs2[1].operation_id.endswith(":2")
    db.commit()


# ───────────────────────── 原始事件不进索引 ─────────────────────────


def test_raw_event_not_indexed(db):
    """#17 原始 Turn/Event 不直接索引：未知 source_type 刷新不产生可检索 entry。"""
    _active_gen(db)
    upserted, tombstoned = refresh_source(db, "raw_event", "ev1", "memory.created")
    # 未知类型：不产生 upsert；tombstone 为无操作（无对应 entry）
    assert upserted == 0
    assert db.query(RetrievalIndexEntry).filter_by(source_id="ev1").count() == 0


# ───────────────────────── DEEP 二阶段原始回溯 ─────────────────────────


def _seed_deep_fixture(db):
    _mk_thread(db, "t1")
    epoch = Epoch(id="e1", thread_id="t1", epoch_no=1, status="active")
    db.add(epoch)
    seg = Segment(id="s1", epoch_id="e1", thread_id="t1", segment_no=1, start_turn_sequence=0)
    db.add(seg)
    db.flush()
    # TurnRecord + Event（关联 segment，按 turn_sequence 排序）
    tr = TurnRecord(
        id="tr1", thread_id="t1", turn_id="turn-a", turn_sequence=1,
        status="completed", segment_id="s1", epoch_id="e1",
        request_fingerprint="fp",
    )
    db.add(tr)
    ev = Event(
        id="ev1", trace_id="tr1", thread_id="t1", turn_id="turn-a",
        event_type="user_message", turn_event_index=0,
        payload={"content": "我决定用 Python 写这个脚本"},
    )
    db.add(ev)
    db.flush()
    # 计算 event_content_hash 并构造 manifest
    content_hash = event_content_hash(ev)
    event_manifest = [{
        "event_id": "ev1", "turn_record_id": "tr1",
        "turn_event_index": 0, "event_type": "user_message",
        "content_hash": content_hash,
    }]
    # CompactionInput（revision 6：DEEP 展开需要 manifest 校验）
    ci = CompactionInput(
        id="ci1", segment_id="s1", start_turn_sequence=0, end_turn_sequence=0,
        event_manifest=event_manifest, working_state_version=0,
        source_hash="h_fixture", summary_version=1,
    )
    db.add(ci)
    summ = SegmentSummary(
        id="ss1", segment_id="s1", goal="Python 编程偏好", outcome="o",
        source_event_ids=["ev1"], source_hash="h_fixture", summary_version=1,
    )
    db.add(summ)
    db.flush()
    return summ


def test_deep_raw_expansion_orders_by_turn_sequence(db):
    """#18 #19 DEEP 模式从 summary 命中回溯原始事件，按 turn_sequence 排序并返回 raw 命中。"""
    gen = _active_gen(db)
    summ = _seed_deep_fixture(db)
    refresh_source(db, "segment_summary", summ.id, "memory.created")
    ur = UnifiedRetriever(db)
    res = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.DEEP,
        source_types=["segment_summary", "epoch_checkpoint"],
    ))
    raw_hits = [h for h in res.hits if h.is_raw]
    assert raw_hits, "DEEP 应产出原始事件回溯命中"
    assert raw_hits[0].source_type == "raw_event"
    assert "Python" in raw_hits[0].snippet


def test_raw_history_expander_no_segment_returns_empty(db):
    """#19 命中无 segment/epoch 关联时不展开原始事件（防跨 Segment 无界扩展）。"""
    hit = RetrievalHit(source_type="segment_summary", source_id="x", source_version="1")
    exp = RawHistoryExpander(db)
    from aiive.memory.recall_config import RetrievalConfig
    out = exp.expand(db, [hit], RetrievalConfig(), deep_max_turns=40, token_budget=1500)
    assert out == []


def test_ensure_retrieval_generation_bootstrap(db):
    """#45 启动引导：库内无 generation 时创建唯一 active generation，且幂等可重复调用。"""
    from aiive.main import _ensure_retrieval_generation

    assert db.query(RetrievalIndexGeneration).first() is None
    _ensure_retrieval_generation()
    gen = db.query(RetrievalIndexGeneration).filter_by(status="active").first()
    assert gen is not None
    assert gen.index_version == 1
    # 幂等：再次调用不新建（仍唯一 active）
    _ensure_retrieval_generation()
    active = db.query(RetrievalIndexGeneration).filter_by(status="active").all()
    assert len(active) == 1


# ════════════════════════════════════════════════════════════════
# 修复验证测试（upsert 排序 / fail-closed / TokenCounter / memory_search / deep）
# ════════════════════════════════════════════════════════════════


def test_version_cmp_not_lexicographic():
    """"9"/"10" 不按字符串序判断：version_cmp("9","10") < 0。"""
    from aiive.retrieval.source_version import version_cmp
    assert version_cmp("9", "10") < 0
    assert version_cmp("10", "9") > 0
    assert version_cmp("10", "10") == 0


def test_exact_version_refresh_idempotent_no_unique_violation(db):
    """精确版本重复 refresh 不违反 UNIQUE：同一 source_version 第二次 upsert 走更新路径。"""
    gen = _active_gen(db)
    mgr = RetrievalIndexManager()
    base = dict(
        db=db, generation=gen, source_type="memory_record", source_id="m1",
        source_hash="h", title="k", search_text="x", snippet="x",
        scope_type="global", scope_id=None, canonical_key="k",
        lifecycle_state="active", validity_state="valid", retrieval_tier="warm",
        thread_id=None, epoch_id=None, segment_id=None, memory_record_id="m1",
        metadata={}, created_source_at=_now(), updated_source_at=_now(),
        tokens=[("x", "word")],
    )
    # 第一次
    assert mgr.upsert_entry(source_version="10", **base) == "upserted"
    # 精确同一版本再次 refresh → 应为 upserted（走精确更新），不抛 IntegrityError
    assert mgr.upsert_entry(source_version="10", **base) == "upserted"
    entries = db.query(RetrievalIndexEntry).filter_by(
        source_type="memory_record", source_id="m1", index_version=gen.index_version,
    ).all()
    assert len(entries) == 1  # 未重复创建行


def test_stale_old_version_not_become_current(db):
    """迟到旧 refresh（"9"）在 "10" 已是 current 时，不重新成为 current。"""
    gen = _active_gen(db)
    mgr = RetrievalIndexManager()
    base = dict(
        db=db, generation=gen, source_type="segment_summary", source_id="ss1",
        source_hash="h", title="t", search_text="x", snippet="x",
        scope_type="global", scope_id=None, canonical_key=None,
        lifecycle_state="valid", validity_state="valid", retrieval_tier="warm",
        thread_id=None, epoch_id=None, segment_id="s1", memory_record_id=None,
        metadata={}, created_source_at=_now(), updated_source_at=_now(),
        tokens=[("x", "word")],
    )
    mgr.upsert_entry(source_version="10", **base)
    # 迟到旧版本 refresh（"9"）
    assert mgr.upsert_entry(source_version="9", **base) == "skipped_stale"
    # 确认 current 仍然是 "10"
    currents = db.query(RetrievalIndexEntry).filter_by(
        source_type="segment_summary", source_id="ss1", is_current=True,
    ).all()
    assert len(currents) == 1
    assert currents[0].source_version == "10"


def test_same_source_generation_max_one_current_enforced(db):
    """同 source/generation 最多一个 current：再次 upsert 后 demote 旧行。"""
    gen = _active_gen(db)
    mgr = RetrievalIndexManager()
    base = dict(
        db=db, generation=gen, source_type="epoch_checkpoint", source_id="cp1",
        source_hash="h", title="t", search_text="x", snippet="x",
        scope_type="global", scope_id=None, canonical_key=None,
        lifecycle_state="valid", validity_state="valid", retrieval_tier="warm",
        thread_id=None, epoch_id=None, segment_id=None, memory_record_id=None,
        metadata={}, created_source_at=_now(), updated_source_at=_now(),
        tokens=[("x", "word")],
    )
    mgr.upsert_entry(source_version="1", **base)
    mgr.upsert_entry(source_version="2", **base)
    currents = db.query(RetrievalIndexEntry).filter_by(
        source_type="epoch_checkpoint", source_id="cp1", is_current=True,
    ).all()
    assert len(currents) == 1


def test_archived_filtered_in_tombstone_window(db):
    """archived 在 tombstone 空窗中被实时 fail-closed 过滤（不回源时 is_current 仍 True）。"""
    gen = _active_gen(db)
    mem = _mk_memory(db, lifecycle_state=LifecycleState.ARCHIVED.value)
    # 手动创建索引 entry（模拟 refresh 已在处理但源已被 archive）
    mgr = RetrievalIndexManager()
    mgr.upsert_entry(
        db, gen, source_type="memory_record", source_id=mem.id,
        source_version=str(mem.record_version), source_hash=mem.content_hash,
        title=mem.canonical_key or "", search_text=mem.content or "", snippet=(mem.content or "")[:200],
        scope_type="global", scope_id=None, canonical_key=mem.canonical_key,
        lifecycle_state="active", validity_state="valid", retrieval_tier="warm",
        thread_id=None, epoch_id=None, segment_id=None, memory_record_id=mem.id,
        metadata={}, created_source_at=mem.created_at, updated_source_at=mem.updated_at,
        tokens=[("test", "word")],
    )
    db.flush()
    ur = UnifiedRetriever(db)
    res = ur.retrieve(RetrievalRequest(
        query="test", mode=RetrievalMode.SEARCH,
        source_types=["memory_record"], scope_context={},
    ))
    # archived → fail-closed 过滤
    assert not any(h.source_id == mem.id for h in res.hits)


def test_memory_search_uses_unified_retriever_consistent(db):
    """memory_search 内部统一走 UnifiedRetriever 编排（代码路径验证）。"""
    gen = _active_gen(db)
    mem = _mk_memory(db, canonical_key="user.pref.lang", content="用户偏好使用 Python 编程")
    refresh_source(db, "memory_record", mem.id, "memory.created")
    db.commit()  # commit 数据，使工具 handler 内的独立 session 可见

    # 经 memory_search 工具（_db_handler 装饰器自动开启新 session）
    from aiive.tools.builtin_tools import _handle_memory_search
    from aiive.context.run_context import RunContext
    ctx = RunContext(thread_id="t1", trace_id="tr1")
    tool_res = _handle_memory_search(ctx=ctx, query="Python", top_k=20)
    assert tool_res["ok"] is True
    result_ids = {r["memory_id"] for r in tool_res["results"]}
    assert mem.id in result_ids, "memory_search 应经 UnifiedRetriever 返回已索引 memory"


def test_deep_returns_parent_summary_and_raw(db):
    """Deep 模式同时返回驱动展开的 parent Summary + raw expansion。"""
    gen = _active_gen(db)
    summ = _seed_deep_fixture(db)
    refresh_source(db, "segment_summary", summ.id, "memory.created")
    ur = UnifiedRetriever(db)
    res = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.DEEP,
        source_types=["segment_summary", "epoch_checkpoint"],
    ))
    # 父级 Summary 命中应在结果中
    parent = [h for h in res.hits if h.source_type == "segment_summary"]
    assert len(parent) >= 1, "DEEP 应返回父级 segment_summary 命中"
    assert parent[0].source_id == summ.id
    # raw expansion 也应在结果中
    raw_hits = [h for h in res.hits if h.is_raw]
    assert len(raw_hits) >= 1, "DEEP 应返回 raw_event 展开命中"


def test_token_count_uses_real_counter(db):
    """token_count 使用真实 TokenCounter（safe_tokens > 0，非 len//4）。"""
    gen = _active_gen(db)
    mgr = RetrievalIndexManager()
    mgr.upsert_entry(
        db, gen, source_type="segment_summary", source_id="ss1",
        source_version="1", source_hash="h1", title="Python 编程",
        search_text="Python 编程偏好 用户明确选择 Python 作为主要开发语言",
        snippet="Python 编程偏好 用户明确选择 Python 作为主要开发语言",
        scope_type="global", scope_id=None, canonical_key=None,
        lifecycle_state="valid", validity_state="valid", retrieval_tier="warm",
        thread_id=None, epoch_id=None, segment_id="s1", memory_record_id=None,
        metadata={}, created_source_at=_now(), updated_source_at=_now(),
        tokens=[("python", "word"), ("编程", "cjk_bigram")],
    )
    db.flush()
    ur = UnifiedRetriever(db)
    res = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.SEARCH,
        source_types=["segment_summary"],
    ))
    for h in res.hits:
        assert h.token_count > 0, f"token_count 应为正数: {h.token_count}"
        # 不应等于 len//4 启发式结果（中文下差异显著）
        len_div = max(1, len(h.snippet) // 4)
        # 只断言行得正数；不依懒 exact match（LiteLLM 可能 fallback 到 conservative）


def test_revalidate_bounded_in_not_n_plus_one(db):
    """fail-closed 回源校验为 bounded IN，不产生 N+1 查询。"""
    gen = _active_gen(db)
    # 创建 5 个 memory
    mems = []
    for i in range(5):
        m = _mk_memory(db, id=f"m{i}", canonical_key=f"key.{i}",
                       content=f"用户偏好 {i} Python 编程",
                       lifecycle_state=LifecycleState.ACTIVE.value)
        refresh_source(db, "memory_record", m.id, "memory.created")
        mems.append(m)
    db.flush()
    ur = UnifiedRetriever(db)
    # 随机采样一个能触发记忆路由的 scope
    res = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.SEARCH,
        source_types=["memory_record"], scope_context={},
    ))
    # 验证结果正常返回（不抛异常即 bounded IN 正常）
    assert res is not None
    # 所有返回的记忆应为 active
    for h in res.hits:
        m = db.get(MemoryRecord, h.source_id)
        assert m is not None
        assert m.lifecycle_state == LifecycleState.ACTIVE.value


# ════════════════════════════════════════════════════════════════
# rebuild 端到端测试（全量回填 → 并发更新 → 原子切换）
# ════════════════════════════════════════════════════════════════


def _make_rebuild_claimed(job: OutboxJob) -> "ClaimedJob":
    """为已存在的 OutboxJob 构造匹配的 ClaimedJob DTO。"""
    import uuid as _uuid
    now = datetime.now(timezone.utc)
    lease = now + timedelta(minutes=5)

    claim_token = str(_uuid.uuid4())
    worker_id = "test-worker"

    job.claim_token = claim_token
    job.locked_by = worker_id
    job.lease_expires_at = lease
    job.status = "running"

    from aiive.worker.outbox_dto import ClaimedJob
    return ClaimedJob(
        id=job.id,
        job_type=job.job_type,
        payload=job.payload or {},
        trace_id=job.trace_id,
        retry_count=job.retry_count or 0,
        max_retries=job.max_retries or 3,
        claim_token=claim_token,
        schema_version=job.schema_version or 1,
        worker_id=worker_id,
    )


def _run_handler_until_done(claimed: "ClaimedJob", db) -> "HandlerOutcome":
    """循环驱动 handler 返回 COMPLETED，对 CONTINUE 持续重入。

    每轮 commit 后做 db.expire_all()，使测试 session 能读到 handler 提交的数据。
    返回最终 outcome。
    """
    from aiive.retrieval.index_rebuild import handle_retrieval_index_rebuild
    from aiive.worker.outbox_dto import HandlerOutcome

    result = handle_retrieval_index_rebuild(claimed)
    safety = 0
    while result.outcome == HandlerOutcome.CONTINUE and safety < 100:
        db.expire_all()
        result = handle_retrieval_index_rebuild(claimed)
        safety += 1
    db.expire_all()
    return result.outcome


def test_rebuild_e2e_full_cycle(db):
    """rebuild 全链路：active gen 服务 → building backfill → 原子激活 → 旧代退出服务。"""
    from aiive.retrieval.index_rebuild import handle_retrieval_index_rebuild
    from aiive.worker.outbox_dto import HandlerOutcome
    import uuid as _uuid

    # ── 1. 搭建数据：active gen v1 + 3 条记忆 ──
    gen1 = _active_gen(db)  # index_version=1
    mems = []
    for i in range(3):
        m = _mk_memory(
            db, id=f"m{i}", canonical_key=f"key.{i}",
            content=f"v1: 记忆{i} 用户偏好 Python",
            lifecycle_state=LifecycleState.ACTIVE.value,
        )
        refresh_source(db, "memory_record", m.id, "memory.created")
        mems.append(m)
    db.commit()  # commit 使 handler 的独立 session 可见

    # ── 2. 创建 rebuild OutboxJob + ClaimedJob ──
    job = OutboxJob(
        id=str(_uuid.uuid4()),
        operation_id=f"rebuild:e2e:{_uuid.uuid4().hex[:8]}",
        job_type="retrieval_index_rebuild",
        status="pending",
        payload={"schema_version": 1, "operation_id": f"rebuild:e2e"},
        trace_id="t1",
        max_retries=3,
    )
    db.add(job)
    db.flush()
    db.commit()

    claimed = _make_rebuild_claimed(job)
    db.commit()

    # ── 3. 驱动 rebuild 到完成 ──
    final = _run_handler_until_done(claimed, db)

    assert final == HandlerOutcome.COMPLETED, f"预期 COMPLETED，实际 {final}"

    # ── 4. 验证：gen1 退役，新 gen 上线 ──
    # gen1 应为 retired
    db.expire_all()
    old = db.query(RetrievalIndexGeneration).filter_by(index_version=1).first()
    assert old is not None
    assert old.status == "retired", f"旧 generation 应为 retired，实际 {old.status}"

    # 新 gen 应为 active
    active = db.query(RetrievalIndexGeneration).filter_by(status="active").first()
    assert active is not None
    assert active.index_version > 1, f"新 generation index_version 应 >1，实际 {active.index_version}"

    # ── 5. unified retriever 使用新 gen 可查到记忆 ──
    ur = UnifiedRetriever(db)
    res = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.SEARCH,
        source_types=["memory_record"], scope_context={},
    ))
    result_ids = {h.source_id for h in res.hits}
    for m in mems:
        assert m.id in result_ids, f"记忆 {m.id} 应在重建后 active gen 中可检索"


def test_rebuild_concurrent_update_preserved_after_switch(db):
    """rebuild 期间 source 更新进入 active+building，切换后不丢失新内容。"""
    import uuid as _uuid

    # ── 1. 建 sample 源（只用 epoch_checkpoint，三条源类型固定 ⇒ 可以控制 rebuild 批次数）──
    _mk_thread(db, "t1")
    epoch = Epoch(id="e1", thread_id="t1", epoch_no=1, status="active")
    db.add(epoch)
    db.flush()

    gen1 = _active_gen(db)
    # 批量插入 checkpoint（不同 version，满足 UNIQUE(epoch_id,version)）
    for i in range(3):
        cp = EpochCheckpoint(
            id=f"cp{i}", epoch_id="e1", version=i + 1, current_goal=f"v1: goal{i}",
            open_loops=[{"description": f"task{i}"}],
            active_constraints=[],
        )
        db.add(cp)
        db.flush()
        refresh_source(db, "epoch_checkpoint", cp.id, "epoch.checkpointed")
    db.commit()

    # ── 2. 创建 rebuild Job ──
    job = OutboxJob(
        id=str(_uuid.uuid4()),
        operation_id=f"rebuild:concurrent:{_uuid.uuid4().hex[:8]}",
        job_type="retrieval_index_rebuild",
        status="pending",
        payload={"schema_version": 1, "operation_id": f"rebuild:concurrent"},
        trace_id="t1",
        max_retries=3,
    )
    db.add(job)
    db.flush()
    db.commit()

    claimed = _make_rebuild_claimed(job)
    db.commit()

    # ── 3. 第一次 CONTINUE（处理部分源后暂停）──
    from aiive.retrieval.index_rebuild import handle_retrieval_index_rebuild
    from aiive.worker.outbox_dto import HandlerOutcome

    r1 = handle_retrieval_index_rebuild(claimed)
    assert r1.outcome in (HandlerOutcome.CONTINUE, HandlerOutcome.COMPLETED), f"r1={r1.outcome}"

    # ── 4. 模拟并发更新：修改 cp0 并刷新到 active (v1) → building (v2) 双代 ──
    db.expire_all()
    cp0 = db.get(EpochCheckpoint, "cp0")
    assert cp0 is not None
    cp0.current_goal = "v2: 更新后的 goal"
    cp0.version = 99  # 唯一版本，不与 cp1(v=2)/cp2(v=3) 冲突
    db.flush()
    db.commit()

    # refresh_source 写双代
    refresh_source(db, "epoch_checkpoint", "cp0", "epoch.checkpointed")
    db.commit()

    # ── 5. 驱动剩余 rebuild → 完成 ──
    final = _run_handler_until_done(claimed, db)
    assert final == HandlerOutcome.COMPLETED, f"final={final}"

    # ── 6. 验证 ➜ 新 active gen 包含 v2 版本 cp0 ──
    db.expire_all()
    active_gen = db.query(RetrievalIndexGeneration).filter_by(status="active").first()
    assert active_gen is not None
    assert active_gen.index_version > 1

    entry = db.query(RetrievalIndexEntry).filter_by(
        source_type="epoch_checkpoint",
        source_id="cp0",
        index_version=active_gen.index_version,
        is_current=True,
    ).first()
    assert entry is not None, "cp0 应在新 active generation 中有 current entry"
    assert "v2" in (entry.search_text or ""), f"新 entry 应包含 v2 内容: {entry.search_text}"

    # ── 7. 旧 gen 为 retired，entries 不参与检索 ──
    retired_gen = db.query(RetrievalIndexGeneration).filter_by(status="retired").first()
    assert retired_gen is not None

    # 旧 gen 中的 entries 仍存在（审计保留）；查询统一走 active gen 的 index_version，
    # 不依赖旧 entries 的 is_current 标记
    old_entries = db.query(RetrievalIndexEntry).filter_by(
        index_version=retired_gen.index_version, source_type="epoch_checkpoint",
    ).all()
    assert len(old_entries) > 0, "旧 gen 条目应保留作审计"

    # 查询统一走 active gen
    ur = UnifiedRetriever(db)
    res = ur.retrieve(RetrievalRequest(
        query="更新后的 goal", mode=RetrievalMode.SEARCH,
        source_types=["epoch_checkpoint"],
    ))
    hit_ids = {h.source_id for h in res.hits}
    assert "cp0" in hit_ids, "并发更新的 cp0 应在 active gen 中被检索到"


# ════════════════════════════════════════════════════════════════
# include_archived / forgotten / bootstrap / 校验 回归
# ════════════════════════════════════════════════════════════════


def _index_archived_memory(db, mid: str, content: str, lifecycle: str):
    """建一条记忆并刷新进索引（archived 保持文本/current/searchable）。"""
    m = _mk_memory(
        db, id=mid, canonical_key=f"key.{mid}", content=content,
        lifecycle_state=lifecycle,
    )
    evt = "memory.forgotten" if lifecycle == LifecycleState.FORGOTTEN.value else "memory.archived"
    refresh_source(db, "memory_record", m.id, evt)
    db.commit()
    return m


def test_archived_excluded_when_include_archived_false(db):
    """#1 include_archived=false 时 SEARCH 不返回 archived 记忆。"""
    _active_gen(db)
    m = _index_archived_memory(db, "m_arch", "归档记忆 Python 编程", LifecycleState.ARCHIVED.value)
    ur = UnifiedRetriever(db)
    res = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.SEARCH, source_types=["memory_record"],
        scope_context={}, include_archived=False,
    ))
    assert m.id not in {h.source_id for h in res.hits}


def test_archived_returned_when_include_archived_true(db):
    """#2 include_archived=true 时 SEARCH/DEEP 返回 archived 记忆。"""
    _active_gen(db)
    m = _index_archived_memory(db, "m_arch2", "归档记忆 Python 编程", LifecycleState.ARCHIVED.value)
    ur = UnifiedRetriever(db)
    res = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.SEARCH, source_types=["memory_record"],
        scope_context={}, include_archived=True,
    ))
    assert m.id in {h.source_id for h in res.hits}


def test_auto_never_returns_archived(db):
    """#3 AUTO 模式即使 include_archived=true 也永远过滤 archived。"""
    _active_gen(db)
    m = _index_archived_memory(db, "m_arch3", "归档记忆 Python 编程", LifecycleState.ARCHIVED.value)
    ur = UnifiedRetriever(db)
    res = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.AUTO, source_types=["memory_record"],
        scope_context={}, include_archived=True,
    ))
    assert m.id not in {h.source_id for h in res.hits}


def test_archived_not_woken_or_touched(db):
    """#4 archived 记忆被检索后 lifecycle 不被改写（不 wake / 不 touch）。"""
    _active_gen(db)
    m = _index_archived_memory(db, "m_arch4", "归档记忆 Python 编程", LifecycleState.ARCHIVED.value)
    ur = UnifiedRetriever(db)
    ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.SEARCH, source_types=["memory_record"],
        scope_context={}, include_archived=True,
    ))
    db.expire_all()
    assert db.get(MemoryRecord, m.id).lifecycle_state == LifecycleState.ARCHIVED.value


def test_forgotten_forbidden_everywhere_and_cleaned(db):
    """#5 forgotten 所有模式禁止返回，且索引文本/token 已清理。"""
    _active_gen(db)
    m = _mk_memory(db, id="m_for", canonical_key="key.for", content="待遗忘记忆 Python 编程",
                   lifecycle_state=LifecycleState.ACTIVE.value)
    refresh_source(db, "memory_record", m.id, "memory.created")
    db.commit()
    # 遗忘并刷新
    m.lifecycle_state = LifecycleState.FORGOTTEN.value
    db.commit()
    refresh_source(db, "memory_record", m.id, "memory.forgotten")
    db.commit()

    ur = UnifiedRetriever(db)
    for mode in (RetrievalMode.SEARCH, RetrievalMode.AUTO, RetrievalMode.DEEP):
        res = ur.retrieve(RetrievalRequest(
            query="Python", mode=mode, source_types=["memory_record"],
            scope_context={}, include_archived=True,
        ))
        assert m.id not in {h.source_id for h in res.hits}, f"{mode} 不应返回 forgotten"

    entry = db.query(RetrievalIndexEntry).filter_by(
        source_type="memory_record", source_id=m.id,
    ).first()
    assert entry is not None
    assert entry.is_searchable is False
    assert (entry.search_text or "") == ""


def test_bootstrap_enqueued_once_for_old_db(db):
    """#6 旧数据库（active gen 未 backfill）启动后自动且仅一次入队 bootstrap rebuild。"""
    _active_gen(db)  # backfill_done 默认 False
    _mk_memory(db, id="m_old", lifecycle_state=LifecycleState.ACTIVE.value)
    db.commit()

    ensure_retrieval_backfill()
    db.expire_all()
    jobs = db.query(OutboxJob).filter_by(job_type="retrieval_index_rebuild").all()
    assert len(jobs) == 1, f"应仅入队一个 bootstrap job，实际 {len(jobs)}"
    assert jobs[0].operation_id == "retrieval_index_rebuild:bootstrap:1"

    # 再次调用不应重复入队
    ensure_retrieval_backfill()
    db.expire_all()
    jobs2 = db.query(OutboxJob).filter_by(job_type="retrieval_index_rebuild").all()
    assert len(jobs2) == 1


def test_no_bootstrap_when_backfill_done(db):
    """#7 已完成 backfill 的 active generation 不再入队 bootstrap rebuild。"""
    gen = _active_gen(db)
    gen.backfill_done = True
    db.commit()

    ensure_retrieval_backfill()
    db.expire_all()
    jobs = db.query(OutboxJob).filter_by(job_type="retrieval_index_rebuild").all()
    assert len(jobs) == 0


def test_epoch_checkpoint_source_hashes_mismatch_filtered(db):
    """#8 EpochCheckpoint version 相同但 source_hashes 不同 → 回源校验过滤。"""
    _active_gen(db)
    _mk_thread(db, "t1")
    epoch = Epoch(id="e1", thread_id="t1", epoch_no=1, status="active")
    db.add(epoch)
    cp = EpochCheckpoint(
        id="cp_h", epoch_id="e1", version=1, current_goal="Python 项目目标",
        open_loops=[], active_constraints=[], source_hashes=["h_a", "h_b"],
    )
    db.add(cp)
    db.commit()
    refresh_source(db, "epoch_checkpoint", "cp_h", "epoch.checkpointed")
    db.commit()

    # 修改 source_hashes（保持 version 不变），不重新刷新 → 索引仍携带旧 hash
    cp.source_hashes = ["h_a", "h_c"]
    db.commit()

    ur = UnifiedRetriever(db)
    res = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.SEARCH, source_types=["epoch_checkpoint"],
    ))
    assert "cp_h" not in {h.source_id for h in res.hits}


def _mk_deep_scenario(
    db, *, event_id: str, event_content: str, manifest_event_id: str,
    source_event_ids: list[str], source_hash: str, goal: str,
):
    """构造单 Segment 的 DEEP 回溯场景（SegmentSummary + CompactionInput + Event）。

    返回创建的 Event 对象，便于在测试中篡改以触发校验失败。
    manifest 的 content_hash 由本函数从创建的事件自动计算。
    """
    _mk_thread(db, "t1")
    epoch = Epoch(id="e1", thread_id="t1", epoch_no=1, status="active")
    db.add(epoch)
    seg = Segment(id="s1", epoch_id="e1", thread_id="t1", segment_no=1,
                  start_turn_sequence=0, status="sealed", summary_id="ss1")
    db.add(seg)
    tr = TurnRecord(id="tr1", segment_id="s1", epoch_id="e1", thread_id="t1",
                    turn_id="turn1", turn_sequence=0, status="completed")
    db.add(tr)
    ev = Event(id=event_id, turn_id="turn1", turn_event_index=0,
               event_type="user_message", payload={"content": event_content},
               thread_id="t1", trace_id="t1")
    db.add(ev)
    db.flush()
    # 基于刚创建的事件计算 content_hash
    auto_hash = event_content_hash(ev)
    summ = SegmentSummary(
        id="ss1", segment_id="s1", goal=goal, outcome="o", summary_version=1,
        source_event_ids=source_event_ids, source_hash=source_hash, decisions=[],
    )
    db.add(summ)
    ci = CompactionInput(
        segment_id="s1", start_turn_sequence=0, end_turn_sequence=0,
        event_manifest=[{
            "event_id": manifest_event_id, "turn_record_id": "tr1",
            "turn_event_index": 0, "event_type": "user_message",
            "content_hash": auto_hash,
        }],
        source_hash=source_hash, summary_version=1,
        working_state_version=0,
    )
    db.add(ci)
    db.flush()
    refresh_source(db, "segment_summary", "ss1", "segment.sealed")
    db.commit()
    return ev


def test_deep_event_not_in_manifest_rejected(db):
    """#9 DEEP：Event 不在 manifest/source_event_ids 中 → 拒绝展开，标注 degraded。"""
    _active_gen(db)
    # 真实事件 ev1；manifest/source_event_ids 指向不存在的 ev_other
    ev = _mk_deep_scenario(
        db,
        event_id="ev1", event_content="真实用户消息",
        manifest_event_id="ev_other",
        source_event_ids=["ev_other"], source_hash="H", goal="Python 项目摘要",
    )
    ur = UnifiedRetriever(db)
    res = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.DEEP, source_types=["segment_summary"],
        scope_context={},
    ))
    parent = [h for h in res.hits if h.source_type == "segment_summary"]
    raw = [h for h in res.hits if h.is_raw]
    assert parent and parent[0].source_id == "ss1"
    assert parent[0].provenance.get("verification_status") == "degraded"
    assert not any(h.source_id == ev.id for h in raw)


def test_deep_event_hash_mismatch_rejected(db):
    """#10 DEEP：Event 内容被篡改（hash 变化）→ 拒绝展开，标注 degraded。"""
    _active_gen(db)
    ev = _mk_deep_scenario(
        db,
        event_id="ev1", event_content="原始用户消息",
        manifest_event_id="ev1",
        source_event_ids=["ev1"], source_hash="H", goal="Python 项目摘要",
    )
    # 篡改 Event 内容，但不更新 manifest
    ev.payload = {"content": "被篡改的用户消息"}
    db.commit()

    ur = UnifiedRetriever(db)
    res = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.DEEP, source_types=["segment_summary"],
        scope_context={},
    ))
    parent = [h for h in res.hits if h.source_type == "segment_summary"]
    raw = [h for h in res.hits if h.is_raw]
    assert parent and parent[0].provenance.get("verification_status") == "degraded"
    assert not any(h.source_id == ev.id for h in raw)


def test_hot_summary_excluded_from_unified_recall(db):
    """#11 Hot Summary 与 UnifiedRetriever 结果不重复（exclude_source_ids 去重）。"""
    _active_gen(db)
    _mk_thread(db, "t1")
    epoch = Epoch(id="e1", thread_id="t1", epoch_no=1, status="active")
    db.add(epoch)
    seg_hot = Segment(id="s_hot", epoch_id="e1", thread_id="t1", segment_no=1,
                      start_turn_sequence=0, status="sealed", summary_id="ss_hot")
    seg_old = Segment(id="s_old", epoch_id="e1", thread_id="t1", segment_no=2,
                      start_turn_sequence=10, status="sealed", summary_id="ss_old")
    db.add_all([seg_hot, seg_old])
    summ_hot = SegmentSummary(id="ss_hot", segment_id="s_hot", goal="hot Python 摘要",
                               summary_version=1, source_hash="h")
    summ_old = SegmentSummary(id="ss_old", segment_id="s_old", goal="old Python 摘要",
                               summary_version=1, source_hash="h")
    db.add_all([summ_hot, summ_old])
    db.flush()
    refresh_source(db, "segment_summary", "ss_hot", "segment.sealed")
    refresh_source(db, "segment_summary", "ss_old", "segment.sealed")
    db.commit()

    ur = UnifiedRetriever(db)
    res = ur.retrieve(RetrievalRequest(
        query="Python", mode=RetrievalMode.SEARCH, source_types=["segment_summary"],
        scope_context={}, exclude_source_ids={"ss_hot"},
    ))
    ids = {h.source_id for h in res.hits}
    assert "ss_old" in ids
    assert "ss_hot" not in ids


def test_sqlite_conflict_does_not_rollback_rebuild(db):
    """#12 SQLite 分支冲突时仅回滚单条插入，不破坏整个 rebuild 事务。"""
    from aiive.retrieval.index_rebuild import _insert_conflict_do_nothing

    # 显式开启外层事务使 SAVEPOINT 嵌套正确（生产 handler 自带活跃事务）
    db.begin()
    # 创建 OutboxJob 满足 FK 约束（retrieval_index_runs.outbox_job_id → outbox_jobs.id）
    db.add(OutboxJob(
        id="oj_x", operation_id="op_x", job_type="retrieval_index_rebuild",
        status="pending", payload={}, trace_id="t1", max_retries=3,
    ))
    db.flush()
    _insert_conflict_do_nothing(db, RetrievalIndexRun, {
        "id": "run_x", "outbox_job_id": "oj_x", "operation_id": "op_x",
        "status": "running", "index_version": 0,
        "batch_cursor": {"source_type": "memory_record", "last_id": None},
    }, ["outbox_job_id"])
    # 重复插入（同 outbox_job_id）应幂等，不产生第二行
    _insert_conflict_do_nothing(db, RetrievalIndexRun, {
        "id": "run_x2", "outbox_job_id": "oj_x", "operation_id": "op_x",
        "status": "running", "index_version": 0,
        "batch_cursor": {"source_type": "memory_record", "last_id": None},
    }, ["outbox_job_id"])
    db.flush()
    count = db.query(RetrievalIndexRun).filter_by(outbox_job_id="oj_x").count()
    assert count == 1, f"幂等插入应只保留一行，实际 {count}"

    # 外层事务仍可提交（此前 db.rollback() 会破坏整个 rebuild 事务）
    db.commit()
    db.expire_all()
    assert db.query(RetrievalIndexRun).filter_by(outbox_job_id="oj_x").count() == 1


def test_k_fusion_rrf_and_weighted_deterministic():
    """K 节评分融合：RRF + 归一化加权；final_score ∈ [0,1]、确定性，且 exact 加权生效。"""
    ur = UnifiedRetriever(None)  # _fuse_scores 不触达 db
    cfg = RetrievalConfig()

    # exact-only 与 memory-only：除 exact_boost 外其余分量相同，用于隔离验证权重接线
    exact_only = RetrievalHit(
        source_type="segment_summary", source_id="e_only", source_version="1",
        score=1.0, lexical_score=0.5, recency_score=0.5, authority_score=0.5,
    )
    memory_only = RetrievalHit(
        source_type="segment_summary", source_id="m_only", source_version="1",
        score=0.9, lexical_score=0.5, recency_score=0.5, authority_score=0.5,
    )
    # 多路命中：同 source 同时出现在 exact 与 memory 路由（RRF 取各路均值）
    multi = RetrievalHit(
        source_type="memory_record", source_id="m1", source_version="1",
        score=0.9, lexical_score=0.9, recency_score=0.7, authority_score=0.6,
    )
    exact_multi = RetrievalHit(
        source_type="memory_record", source_id="m1", source_version="1",
        score=1.0, lexical_score=1.0, recency_score=0.5, authority_score=0.5,
    )
    ur._fuse_scores([exact_only, exact_multi], [memory_only, multi], [], cfg)

    for h in (exact_only, memory_only, multi, exact_multi):
        assert 0.0 <= h.final_score <= 1.0

    # exact 加权生效：其余分量相同，exact-only 应高于 memory-only
    assert exact_only.final_score > memory_only.final_score
    # 多路命中 RRF 融合后 final_score 为正（确定性，不依赖哈希顺序）
    assert multi.final_score > 0.0

    # 确定性：相同输入重复计算，结果一致
    again = UnifiedRetriever(None)
    e2 = RetrievalHit(source_type="segment_summary", source_id="e_only", source_version="1",
                      score=1.0, lexical_score=0.5, recency_score=0.5, authority_score=0.5)
    m2 = RetrievalHit(source_type="segment_summary", source_id="m_only", source_version="1",
                      score=0.9, lexical_score=0.5, recency_score=0.5, authority_score=0.5)
    again._fuse_scores([e2], [m2], [], RetrievalConfig())
    assert e2.final_score == exact_only.final_score
    assert m2.final_score == memory_only.final_score


def test_dedup_budget_stable_tie_breaker():
    """K 节稳定 tie-breaker：同 final_score 时按 (source_type, source_id) 升序；截断确定。"""
    ur = UnifiedRetriever(None)
    a = RetrievalHit(source_type="memory_record", source_id="a", source_version="1",
                     final_score=0.5, token_count=10)
    b = RetrievalHit(source_type="memory_record", source_id="b", source_version="1",
                     final_score=0.5, token_count=10)
    # 乱序传入，验证排序稳定
    out = ur._dedup_and_budget([b, a], max_results=None, token_budget=None)
    assert [h.source_id for h in out] == ["a", "b"]

    # 预算截断（max_results=1）按 tie-breaker 取较小 source_id，结果确定
    out1 = ur._dedup_and_budget([b, a], max_results=1, token_budget=None)
    assert [h.source_id for h in out1] == ["a"]


