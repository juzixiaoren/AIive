"""Phase 4 缺口回归测试（G1 / G2 / G3）。

G2 — AutomaticRecallEngine.include_sleeping：
    exact / fts 在 include_sleeping=True 时放宽到 IN(active, sleeping)；
    episode 兜底路由始终仅 active（避免陈旧情节记忆被重新带回上下文）。

G1 — MemoryAccessTracker 真实接线：
    context_assembler 在「记忆真正注入模型上下文」的唯一确定点 touch pack.items；
    memory_search 工具在返回前 touch 实际返回的记忆 id（含 sleeping→wake）。

G3 — MemoryWriteService.promote / execute_maintenance 委托 MemoryLifecycleService：
    走共享 executor（版本 bump + lineage + 投影），消除无版本校验旁路。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from aiive.context.run_context import RunContext
from aiive.db.base import SessionLocal
from aiive.db.models import MemoryLineage, MemoryProposal, MemoryRecord, OutboxJob
from aiive.memory.automatic_recall import AutomaticRecallEngine
from aiive.memory.memory_access_tracker import MemoryAccessTracker
from aiive.memory.memory_types import LifecycleState, LineageOperation
from aiive.memory.memory_write_service import MemoryWriteService
from aiive.memory.recall_config import RecallConfig
from aiive.memory.recall_models import MemoryRecallItem, MemoryRecallPack, MemoryRecallRequest, ScopeContext
from aiive.tools.builtin_tools import _handle_memory_search


def _now():
    return datetime.now(timezone.utc)


def _mk_record(db, **kwargs) -> MemoryRecord:
    """构造一条 MemoryRecord（未 commit）。"""
    defaults = dict(
        id=str(uuid.uuid4()),
        memory_type="fact",
        canonical_key="test.key",
        scope_type="global",
        scope_id=None,
        content="content",
        lifecycle_state=LifecycleState.CANDIDATE.value,
        validity_state="valid",
        confidence=0.5,
        importance=0.5,
        retention_policy="normal",
        record_version=1,
        pinned=False,
        stability="contextual",
        observed_at=_now(),
        created_at=_now(),
    )
    defaults.update(kwargs)
    rec = MemoryRecord(**defaults)
    db.add(rec)
    db.flush()
    return rec


# ───────────────────────── G2: include_sleeping 召回 ─────────────────────────


def test_recall_fts_returns_sleeping_when_included(db):
    active = _mk_record(db, lifecycle_state=LifecycleState.ACTIVE.value,
                        content="量子计算使用超导比特实现高精度运算",
                        canonical_key="tech.quantum.active")
    sleeping = _mk_record(db, lifecycle_state=LifecycleState.SLEEPING.value,
                          content="量子计算在低温下展现长程相干性",
                          canonical_key="tech.quantum.sleep")
    db.commit()

    engine = AutomaticRecallEngine(db, RecallConfig())
    pack, _ = engine.recall(
        MemoryRecallRequest(query="量子计算", scope_context=ScopeContext()),
        include_sleeping=True,
    )
    ids = {it.memory_id for it in pack.items}
    assert active.id in ids
    assert sleeping.id in ids


def test_recall_fts_excludes_sleeping_by_default(db):
    active = _mk_record(db, lifecycle_state=LifecycleState.ACTIVE.value,
                        content="量子计算使用超导比特实现高精度运算",
                        canonical_key="tech.quantum.active")
    sleeping = _mk_record(db, lifecycle_state=LifecycleState.SLEEPING.value,
                          content="量子计算在低温下展现长程相干性",
                          canonical_key="tech.quantum.sleep")
    db.commit()

    engine = AutomaticRecallEngine(db, RecallConfig())
    pack, _ = engine.recall(
        MemoryRecallRequest(query="量子计算", scope_context=ScopeContext()),
        include_sleeping=False,
    )
    ids = {it.memory_id for it in pack.items}
    assert active.id in ids
    assert sleeping.id not in ids


def test_recall_exact_returns_sleeping_when_included(db):
    active = _mk_record(db, lifecycle_state=LifecycleState.ACTIVE.value,
                        canonical_key="project.aiive.backend")
    sleeping = _mk_record(db, lifecycle_state=LifecycleState.SLEEPING.value,
                          canonical_key="project.aiive.backend")
    db.commit()

    engine = AutomaticRecallEngine(db, RecallConfig())
    pack, _ = engine.recall(
        MemoryRecallRequest(query="project.aiive.backend", scope_context=ScopeContext()),
        include_sleeping=True,
    )
    ids = {it.memory_id for it in pack.items}
    assert active.id in ids
    assert sleeping.id in ids


def test_recall_exact_excludes_sleeping_by_default(db):
    active = _mk_record(db, lifecycle_state=LifecycleState.ACTIVE.value,
                        canonical_key="project.aiive.backend")
    sleeping = _mk_record(db, lifecycle_state=LifecycleState.SLEEPING.value,
                          canonical_key="project.aiive.backend")
    db.commit()

    engine = AutomaticRecallEngine(db, RecallConfig())
    pack, _ = engine.recall(
        MemoryRecallRequest(query="project.aiive.backend", scope_context=ScopeContext()),
        include_sleeping=False,
    )
    ids = {it.memory_id for it in pack.items}
    assert active.id in ids
    assert sleeping.id not in ids


def test_recall_episode_excludes_sleeping_even_when_requested(db):
    """episode 兜底路由始终仅 active：即便 include_sleeping=True，sleeping 情节记忆也不进入 pack。"""
    active_ep = _mk_record(db, lifecycle_state=LifecycleState.ACTIVE.value,
                           memory_type="episodic",
                           content="今日量子计算实验顺利完成验收",
                           canonical_key="ep.active")
    # sleeping 情节记忆：内容与查询无 token 重叠 → fts 不会命中，仅可能经 episode 路由；
    # 若它出现在 pack，只能来自 episode 路由，据此验证 episode 不收录 sleeping。
    sleeping_ep = _mk_record(db, lifecycle_state=LifecycleState.SLEEPING.value,
                             memory_type="episodic",
                             content="上周去公园散步天气晴朗",
                             canonical_key="ep.sleep")
    db.commit()

    engine = AutomaticRecallEngine(db, RecallConfig())
    pack, _ = engine.recall(
        MemoryRecallRequest(query="量子计算实验", scope_context=ScopeContext()),
        include_sleeping=True,
    )
    ids = {it.memory_id for it in pack.items}
    assert active_ep.id in ids
    assert sleeping_ep.id not in ids


# ───────────────────────── G1: AccessTracker 真实接线 ─────────────────────────


def test_memory_search_touches_returned_memory_ids(db, monkeypatch):
    """memory_search 返回前 touch 实际返回的记忆 id（J.4/J.6）。"""
    active = _mk_record(db, lifecycle_state=LifecycleState.ACTIVE.value,
                        content="量子计算使用超导比特实现高精度运算",
                        canonical_key="tech.quantum.active")
    sleeping = _mk_record(db, lifecycle_state=LifecycleState.SLEEPING.value,
                          content="量子计算在低温下展现长程相干性",
                          canonical_key="tech.quantum.sleep")
    db.commit()

    captured: list[list[str]] = []
    orig_touch = MemoryAccessTracker.touch

    def _spy(self, memory_ids, now=None):
        captured.append(list(memory_ids))
        return orig_touch(self, memory_ids, now)

    monkeypatch.setattr(MemoryAccessTracker, "touch", _spy)

    ctx = RunContext(thread_id="t1", trace_id="tr1")
    result = _handle_memory_search(ctx=ctx, query="量子计算")
    returned_ids = {r["memory_id"] for r in result.get("results", [])}

    assert captured, "memory_search 未调用 AccessTracker.touch"
    touched = {mid for batch in captured for mid in batch}
    assert active.id in touched
    assert sleeping.id in touched
    # 仅 touch 真正返回的记忆，不 touch 未返回的候选
    assert touched <= returned_ids


def test_memory_search_wakes_returned_sleeping_memory(db):
    """include_sleeping=True 回到的 sleeping 记忆被工具返回 → touch 触发 wake（J.4 闭环）。"""
    sleeping = _mk_record(db, lifecycle_state=LifecycleState.SLEEPING.value,
                          content="量子计算低温相干特性显著",
                          canonical_key="tech.q.sleep")
    _mk_record(db, lifecycle_state=LifecycleState.ACTIVE.value,
               content="量子计算超导比特体系成熟",
               canonical_key="tech.q.active")
    db.commit()

    ctx = RunContext(thread_id="t1", trace_id="tr1")
    _handle_memory_search(ctx=ctx, query="量子计算")

    s = SessionLocal()
    try:
        fresh = s.get(MemoryRecord, sleeping.id)
        assert fresh.lifecycle_state == LifecycleState.ACTIVE.value
        assert fresh.record_version == 2  # wake bump 了版本
    finally:
        s.close()


def test_context_assembler_touch_only_injected(db, monkeypatch):
    """ContextAssembler 只在「记忆真正注入上下文」的确定点 touch pack.items。"""
    import aiive.runtime.context_assembler as ca_mod

    captured: list[list[str]] = []
    orig_touch = MemoryAccessTracker.touch

    def _spy(self, memory_ids, now=None):
        captured.append(list(memory_ids))
        return orig_touch(self, memory_ids, now)

    monkeypatch.setattr(MemoryAccessTracker, "touch", _spy)

    assembler = ca_mod.ContextAssembler.__new__(ca_mod.ContextAssembler)
    pack = MemoryRecallPack(request_id="r", items=[
        MemoryRecallItem(memory_id="id-1", content="", memory_type="",
                         canonical_key="", scope_type="global"),
        MemoryRecallItem(memory_id="id-2", content="", memory_type="",
                         canonical_key="", scope_type="global"),
    ])
    assembler._touch_injected_memory(pack)

    assert captured == [["id-1", "id-2"]]


def test_context_assembler_touch_handles_empty_pack(db, monkeypatch):
    """空 / None pack 不触达任何记录、不抛异常。"""
    import aiive.runtime.context_assembler as ca_mod

    captured: list[list[str]] = []
    monkeypatch.setattr(MemoryAccessTracker, "touch",
                        lambda self, ids, now=None: captured.append(list(ids)))

    assembler = ca_mod.ContextAssembler.__new__(ca_mod.ContextAssembler)
    assembler._touch_injected_memory(None)                       # None pack
    assembler._touch_injected_memory(MemoryRecallPack(request_id="r"))  # 空 items

    assert captured == []


# ───────────────────────── G3: 委托 MemoryLifecycleService ─────────────────────────


def test_write_service_promote_delegates_to_lifecycle(db):
    """promote 委托 lifecycle：candidate→active，bump 版本 + lineage + 投影。"""
    rec = _mk_record(db, lifecycle_state=LifecycleState.CANDIDATE.value,
                     canonical_key="user.name")
    db.commit()
    # 事件写入需真实线程（集成关切）；此处验证委托语义，沿用维护后台的空 thread 路径。
    ctx = RunContext(thread_id="", trace_id="tr1")

    writer = MemoryWriteService(db)
    result = writer.promote(rec.id, ctx)
    db.commit()

    assert result.written is True
    assert result.operation == "promote"

    fresh = db.get(MemoryRecord, rec.id)
    assert fresh.lifecycle_state == LifecycleState.ACTIVE.value
    assert fresh.record_version == 2  # 委托 executor bump 了版本

    lin = db.query(MemoryLineage).filter(
        MemoryLineage.successor_id == rec.id,
        MemoryLineage.operation == LineageOperation.PROMOTE.value,
    ).count()
    assert lin == 1

    # 投影入队（共享 executor 的 enqueue_projection 副作用）
    core = db.query(OutboxJob).filter(
        OutboxJob.job_type == "core_memory_refresh"
    ).count()
    assert core >= 1


def test_write_service_execute_maintenance_sleep_delegates(db):
    """execute_maintenance(sleep) 委托 lifecycle：active→sleeping，bump 版本 + lineage + 投影。"""
    rec = _mk_record(db, lifecycle_state=LifecycleState.ACTIVE.value,
                     canonical_key="user.name")
    db.commit()
    # 事件写入需真实线程（集成关切）；此处验证委托语义，沿用维护后台的空 thread 路径。
    ctx = RunContext(thread_id="", trace_id="tr1")
    prop = MemoryProposal(
        proposed_operation="sleep",
        source_event_ids=[rec.id],
        memory_type=rec.memory_type,
        canonical_key=rec.canonical_key or "",
    )

    writer = MemoryWriteService(db)
    result = writer.execute_maintenance(prop, ctx)
    db.commit()

    assert result.written is True
    assert result.operation == "sleep"

    fresh = db.get(MemoryRecord, rec.id)
    assert fresh.lifecycle_state == LifecycleState.SLEEPING.value
    assert fresh.record_version == 2  # 委托 executor bump 了版本

    core = db.query(OutboxJob).filter(
        OutboxJob.job_type == "core_memory_refresh"
    ).count()
    assert core >= 1
