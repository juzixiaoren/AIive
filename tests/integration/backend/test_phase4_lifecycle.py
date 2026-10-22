"""Phase 4 维护核心组件测试（单元 + 端到端）。

覆盖：MemoryLifecycleService（promote/archive/sleep/merge/wake/skip_stale）、
MemoryAccessTracker（专用 SQL 不碰 updated_at / 不 bump version / sleeping→wake）、
MemoryStore 四条 lane（candidate_due / expired_ephemeral 不受 updated_at 旧 high-water 限制）、
user-required 沿 lineage 追溯，以及 handle_memory_maintenance 三阶段 + CONTINUE 全链路。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from aiive.config import settings
from aiive.db.base import SessionLocal
from aiive.db.models import (
    Event,
    MemoryEvidence,
    MemoryLineage,
    MemoryMaintenanceAction,
    MemoryMaintenanceBatch,
    MemoryMaintenanceInput,
    MemoryMaintenanceRun,
    MemoryProposal,
    MemoryRecord,
    OutboxJob,
)
from aiive.memory.memory_lifecycle_service import MemoryLifecycleService
from aiive.memory.memory_mutation import hashes_for_record
from aiive.memory.memory_store import MemoryStore
from aiive.memory.memory_access_tracker import MemoryAccessTracker
from aiive.memory.memory_types import LifecycleState
from aiive.memory.memory_maintenance_planner import FrozenInput, plan_batch
from aiive.memory.memory_key_registry import MemoryKeySpec
from aiive.memory.recall_config import MaintenanceConfig
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome
from aiive.worker.outbox_handlers import handle_memory_maintenance


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


def _freeze_action(subject, action_type, related=None, reason=None, run_id="r-test", batch_id="b-test", seq=0):
    """构造一个 transient（不持久化）的维护动作，preconditions 取自当前记录态。"""
    st, dh = hashes_for_record(subject)
    pre = {subject.id: {
        "record_version": subject.record_version,
        "record_state_hash": st,
        "decision_hash": dh,
    }}
    if related is not None:
        rst, rdh = hashes_for_record(related)
        pre[related.id] = {
            "record_version": related.record_version,
            "record_state_hash": rst,
            "decision_hash": rdh,
        }
    return MemoryMaintenanceAction(
        run_id=run_id,
        batch_id=batch_id,
        action_sequence=seq,
        subject_memory_record_id=subject.id,
        related_record_ids=[related.id] if related else [],
        action_type=action_type,
        reason_code=reason,
        expected_record_version=subject.record_version,
        preconditions=pre,
        status="pending",
    )


# ───────────────────────── MemoryLifecycleService ─────────────────────────


def test_lifecycle_archive_expired_ephemeral(db, monkeypatch):
    monkeypatch.setattr(settings, "aiive_memory_vector_enabled", True)
    now = _now()
    rec = _mk_record(
        db, lifecycle_state=LifecycleState.ACTIVE.value, retention_policy="ephemeral",
        valid_to=now - timedelta(days=1),
    )
    db.commit()
    svc = MemoryLifecycleService(db)
    status = svc.apply_maintenance_action(_freeze_action(rec, "archive_expired", reason="expired_ephemeral"))
    db.commit()
    assert status == "applied"
    fresh = db.get(MemoryRecord, rec.id)
    assert fresh.lifecycle_state == LifecycleState.ARCHIVED.value
    assert fresh.validity_state == "expired"
    assert db.query(OutboxJob).filter_by(
        job_type="memory_vector_refresh",
        operation_id=f"memory_vector_refresh:{rec.id}:2",
    ).count() == 1


def test_lifecycle_promote_enqueues_vector_refresh(db, monkeypatch):
    monkeypatch.setattr(settings, "aiive_memory_vector_enabled", True)
    rec = _mk_record(db)
    db.commit()

    status = MemoryLifecycleService(db).apply_maintenance_action(
        _freeze_action(rec, "promote", reason="candidate_promoted")
    )
    db.commit()

    assert status == "applied"
    assert db.query(OutboxJob).filter_by(
        job_type="memory_vector_refresh",
        operation_id=f"memory_vector_refresh:{rec.id}:2",
    ).count() == 1


def test_lifecycle_sleep_low_importance_old(db, monkeypatch):
    monkeypatch.setattr(settings, "aiive_memory_vector_enabled", True)
    now = _now()
    old = now - timedelta(days=60)
    rec = _mk_record(
        db, lifecycle_state=LifecycleState.ACTIVE.value, importance=0.1,
        observed_at=old, created_at=old, last_accessed_at=None,
    )
    db.commit()
    svc = MemoryLifecycleService(db)
    status = svc.apply_maintenance_action(_freeze_action(rec, "sleep", reason="cooled_to_sleeping"))
    db.commit()
    assert status == "applied"
    fresh = db.get(MemoryRecord, rec.id)
    assert fresh.lifecycle_state == LifecycleState.SLEEPING.value
    assert db.query(OutboxJob).filter_by(
        job_type="memory_vector_refresh",
        operation_id=f"memory_vector_refresh:{rec.id}:2",
    ).count() == 1


def test_lifecycle_merge_exact_duplicate(db, monkeypatch):
    monkeypatch.setattr(settings, "aiive_memory_vector_enabled", True)
    now = _now()
    r1 = _mk_record(db, lifecycle_state=LifecycleState.CANDIDATE.value, content="dup",
                    content_hash="h-dup", canonical_key="dup.key", created_at=now - timedelta(days=2))
    r2 = _mk_record(db, lifecycle_state=LifecycleState.CANDIDATE.value, content="dup",
                    content_hash="h-dup", canonical_key="dup.key", created_at=now - timedelta(days=1))
    db.commit()
    # r1 id < r2 id（确定性 winner = id 最小者）
    winner, loser = (r1, r2) if r1.id < r2.id else (r2, r1)
    svc = MemoryLifecycleService(db)
    status = svc.apply_maintenance_action(
        _freeze_action(loser, "merge_exact_duplicate", related=winner, reason="duplicate_merged")
    )
    db.commit()
    assert status == "applied"
    fresh_loser = db.get(MemoryRecord, loser.id)
    assert fresh_loser.lifecycle_state == LifecycleState.ARCHIVED.value
    assert fresh_loser.validity_state == "superseded"
    fresh_winner = db.get(MemoryRecord, winner.id)
    assert fresh_winner.lifecycle_state == LifecycleState.CANDIDATE.value
    assert db.query(OutboxJob).filter_by(
        job_type="memory_vector_refresh",
        operation_id=f"memory_vector_refresh:{loser.id}:2",
    ).count() == 1


def test_lifecycle_skip_stale_on_version_mismatch(db):
    rec = _mk_record(db, lifecycle_state=LifecycleState.ACTIVE.value)
    db.commit()
    action = _freeze_action(rec, "sleep", reason="cooled_to_sleeping")
    # 篡改 precondition 的 record_version
    action.preconditions[rec.id]["record_version"] = rec.record_version + 99
    svc = MemoryLifecycleService(db)
    status = svc.apply_maintenance_action(action)
    db.commit()
    assert status == "skip_stale"
    fresh = db.get(MemoryRecord, rec.id)
    assert fresh.lifecycle_state == LifecycleState.ACTIVE.value  # 未变更


def test_lifecycle_no_op_returns_noop(db):
    rec = _mk_record(db)
    db.commit()
    svc = MemoryLifecycleService(db)
    status = svc.apply_maintenance_action(_freeze_action(rec, "no_op", reason="pinned_conflict"))
    db.commit()
    assert status == "no_op"


def test_lifecycle_wake_sleeping(db):
    rec = _mk_record(db, lifecycle_state=LifecycleState.SLEEPING.value)
    db.commit()
    svc = MemoryLifecycleService(db)
    ok = svc.wake(rec.id)
    db.commit()
    assert ok is True
    fresh = db.get(MemoryRecord, rec.id)
    assert fresh.lifecycle_state == LifecycleState.ACTIVE.value
    assert fresh.record_version == 2


# ───────────────────────── MemoryAccessTracker ─────────────────────────


def test_access_tracker_touch_updates_last_accessed_not_updated_at(db):
    rec = _mk_record(db, lifecycle_state=LifecycleState.ACTIVE.value)
    db.commit()
    orig_updated = rec.updated_at
    orig_version = rec.record_version

    MemoryAccessTracker().touch([rec.id])

    s = SessionLocal()
    try:
        fresh = s.get(MemoryRecord, rec.id)
        assert fresh.last_accessed_at is not None
        assert fresh.updated_at == orig_updated  # 专用 SQL 保持 updated_at 不变
        assert fresh.record_version == orig_version  # 不 bump version
        # 普通 touch 不触发投影
        core_jobs = s.query(OutboxJob).filter(
            OutboxJob.job_type == "core_memory_refresh"
        ).count()
        assert core_jobs == 0
    finally:
        s.close()


def test_access_tracker_sleeping_wakes_on_touch(db):
    rec = _mk_record(db, lifecycle_state=LifecycleState.SLEEPING.value)
    db.commit()

    MemoryAccessTracker().touch([rec.id])

    s = SessionLocal()
    try:
        fresh = s.get(MemoryRecord, rec.id)
        assert fresh.lifecycle_state == LifecycleState.ACTIVE.value
        assert fresh.record_version == 2
    finally:
        s.close()


# ───────────────────────── MemoryStore lanes ─────────────────────────


def test_store_candidate_due_ignores_updated_at_high_water(db):
    now = _now()
    old = now - timedelta(days=40)
    # 很久未更新（updated_at 远早于 now）但今天刚到 TTL 的 candidate
    rec = _mk_record(db, lifecycle_state=LifecycleState.CANDIDATE.value,
                     created_at=old, updated_at=old)
    db.commit()
    store = MemoryStore(db)
    seeds = store.select_maintenance_seeds(
        "candidate_due", cutoff_updated_at=now, cursor_time=None, cursor_id=None,
        limit=10, now=now, ttl_days=30,
    )
    assert rec.id in [s.id for s in seeds]


def test_store_expired_ephemeral_ignores_updated_at_high_water(db):
    now = _now()
    old = now - timedelta(days=40)
    rec = _mk_record(db, lifecycle_state=LifecycleState.ACTIVE.value,
                     retention_policy="ephemeral", valid_to=now - timedelta(days=1),
                     updated_at=old)
    db.commit()
    store = MemoryStore(db)
    seeds = store.select_maintenance_seeds(
        "expired_ephemeral", cutoff_updated_at=now, cursor_time=None, cursor_id=None,
        limit=10, now=now, ttl_days=30,
    )
    assert rec.id in [s.id for s in seeds]


def test_store_user_required_trace_via_direct_proposal(db):
    rec = _mk_record(db)
    db.commit()
    # 直接 user_required proposal 指向该记录
    db.add(MemoryProposal(
        proposal_id="prop-ur-1", source_event_ids=["e1"], memory_type="fact",
        canonical_key=rec.canonical_key or "", scope_type=rec.scope_type or "global",
        content="c", execution_mode="user_required", final_memory_id=rec.id,
    ))
    db.commit()
    store = MemoryStore(db)
    protected, source_ids = store.is_user_required_protected(rec.id)
    assert protected is True
    assert "prop-ur-1" in source_ids


# ───────────────────────── Handler 端到端（三阶段 + CONTINUE）─────────────────────────


def _claim_and_run(db, job_id, op_id) -> HandlerOutcome:
    """模拟 OutboxWorker：claim → dispatch → 处理 CONTINUE/RETRY_LATER 直到终态。"""
    outcome = None
    for _ in range(60):
        s = SessionLocal()
        try:
            job = s.get(OutboxJob, job_id)
            st = job.status if job else "missing"
        finally:
            s.close()
        if st in ("completed", "deadletter", "missing"):
            outcome = HandlerOutcome.COMPLETED if st == "completed" else HandlerOutcome.NON_RETRYABLE
            break

        s = SessionLocal()
        try:
            job = s.query(OutboxJob).filter(OutboxJob.id == job_id).with_for_update().first()
            if job is None:
                break
            token = str(uuid.uuid4())
            job.status = "running"
            job.claim_token = token
            job.locked_by = "test-worker"
            job.lease_expires_at = _now() + timedelta(seconds=120)
            s.commit()
        finally:
            s.close()

        claimed = ClaimedJob(
            id=job_id, job_type="memory_maintenance",
            payload={"schema_version": 1, "operation_id": op_id},
            trace_id="t", retry_count=0, max_retries=3,
            claim_token=token, schema_version=1, worker_id="test-worker",
        )
        result = handle_memory_maintenance(claimed)
        outcome = result.outcome
        if outcome in (HandlerOutcome.COMPLETED, HandlerOutcome.NON_RETRYABLE, HandlerOutcome.CLAIM_LOST):
            break
        # CONTINUE / RETRY_LATER → 重新 claim 继续
    return outcome


def test_handler_end_to_end_maintenance(db):
    now = _now()
    old = now - timedelta(days=40)

    # expired ephemeral
    r_exp = _mk_record(db, lifecycle_state=LifecycleState.ACTIVE.value,
                       retention_policy="ephemeral", valid_to=now - timedelta(days=1))
    # stale candidate（超过 TTL）
    r_old = _mk_record(db, lifecycle_state=LifecycleState.CANDIDATE.value,
                      created_at=old, updated_at=old, confidence=0.5)
    # low importance 长期未访问 → sleep
    r_sleep = _mk_record(db, lifecycle_state=LifecycleState.ACTIVE.value, importance=0.1,
                         observed_at=old, created_at=old, last_accessed_at=None)
    # 重复邻域 → merge
    r_d1 = _mk_record(db, lifecycle_state=LifecycleState.CANDIDATE.value, content="dup",
                     content_hash="h-dup", canonical_key="dup.key", created_at=now - timedelta(days=2))
    r_d2 = _mk_record(db, lifecycle_state=LifecycleState.CANDIDATE.value, content="dup",
                     content_hash="h-dup", canonical_key="dup.key", created_at=now - timedelta(days=1))
    db.commit()

    op_id = f"memory_maintenance:all_user_memories:phase4.v1:{now.strftime('%Y-%m-%d')}"
    job = OutboxJob(operation_id=op_id, job_type="memory_maintenance",
                    status="pending", payload={"schema_version": 1}, max_retries=3)
    db.add(job)
    db.commit()
    job_id = job.id

    outcome = _claim_and_run(db, job_id, op_id)
    assert outcome == HandlerOutcome.COMPLETED

    s = SessionLocal()
    try:
        fe = s.get(MemoryRecord, r_exp.id)
        fo = s.get(MemoryRecord, r_old.id)
        fs = s.get(MemoryRecord, r_sleep.id)
        assert fe.lifecycle_state == LifecycleState.ARCHIVED.value
        assert fo.lifecycle_state == LifecycleState.ARCHIVED.value
        assert fs.lifecycle_state == LifecycleState.SLEEPING.value
        # 重复邻域：一胜一负
        d1 = s.get(MemoryRecord, r_d1.id)
        d2 = s.get(MemoryRecord, r_d2.id)
        states = {d1.lifecycle_state, d2.lifecycle_state}
        assert LifecycleState.ARCHIVED.value in states
        # Run 终态
        run = s.query(MemoryMaintenanceRun).filter(
            MemoryMaintenanceRun.outbox_job_id == job_id
        ).first()
        assert run is not None and run.status == "succeeded"
    finally:
        s.close()


def test_handler_no_dirty_does_not_create_run(db):
    """无 dirty memory 时 Run 直接 succeeded，不产生任何 Action。"""
    now = _now()
    op_id = f"memory_maintenance:all_user_memories:phase4.v1:{now.strftime('%Y-%m-%d')}-empty"
    job = OutboxJob(operation_id=op_id, job_type="memory_maintenance",
                    status="pending", payload={"schema_version": 1}, max_retries=3)
    db.add(job)
    db.commit()
    job_id = job.id

    outcome = _claim_and_run(db, job_id, op_id)
    assert outcome == HandlerOutcome.COMPLETED

    s = SessionLocal()
    try:
        run = s.query(MemoryMaintenanceRun).filter(
            MemoryMaintenanceRun.outbox_job_id == job_id
        ).first()
        assert run is not None and run.status == "succeeded"
        action_count = s.query(MemoryMaintenanceAction).filter(
            MemoryMaintenanceAction.run_id == run.id
        ).count()
        assert action_count == 0
    finally:
        s.close()


# ───────────────────────── 单基数冲突 supersede 维护 ─────────────────────────


class _FakeKeyRegistry:
    """测试用：让指定 key 表现为 single-cardinality、非受保护类型。"""

    def __init__(self, single_key: str, memory_type: str = "knowledge") -> None:
        self._single_key = single_key
        self._memory_type = memory_type

    def is_single_cardinality(self, key: str) -> bool:
        return key == self._single_key

    def resolve(self, key: str):
        if key != self._single_key:
            return None
        return MemoryKeySpec(
            canonical_key=key,
            memory_type=self._memory_type,
            cardinality="single",
            conflict_policy="supersede",
            maintenance_policy="persist",
        )


def _mk_frozen(memory_record_id, canonical_key, content_hash, content="c", **kwargs):
    base = dict(
        input_id="inp-" + memory_record_id,
        input_sequence=0,
        memory_record_id=memory_record_id,
        input_role="seed",
        record_version=1,
        canonical_key=canonical_key,
        scope_type="global",
        scope_id=None,
        user_required_protected=False,
        user_required_source_ids=[],
        content=content,
        structured_value=None,
        content_hash=content_hash,
        structured_value_hash=None,
        lifecycle_state=LifecycleState.ACTIVE.value,
        validity_state="valid",
        confidence=0.5,
        importance=0.5,
        retention_policy="normal",
        valid_to=None,
        pinned=False,
        stability="contextual",
        stability_score=None,
        reinforce_count=0,
        last_reinforced_at=None,
        observed_at=_now(),
        created_at=_now(),
        last_accessed_at=None,
        evidence_count=0,
    )
    base.update(kwargs)
    return FrozenInput(**base)


def test_planner_single_cardinality_supersede(monkeypatch):
    """单基数键存在内容冲突 → planner 生成 supersede 动作；受保护类型不产生。"""
    import aiive.memory.memory_maintenance_planner as planner_mod

    key = "test.single_card.key"
    reg = _FakeKeyRegistry(key)
    monkeypatch.setattr(planner_mod, "get_memory_key_registry", lambda: reg)

    w = _mk_frozen("w-id", key, "hash-winner", content="winner-content", confidence=0.9)
    l = _mk_frozen("l-id", key, "hash-loser", content="loser-content", confidence=0.5)
    actions, _, _ = plan_batch("b1", [w, l], MaintenanceConfig(), _now())
    supersede = [a for a in actions if a.action_type == "supersede"]
    assert len(supersede) == 1
    act = supersede[0]
    assert act.subject_memory_record_id == "w-id"
    assert act.related_record_ids == ["l-id"]
    # 受保护类型（user_profile）不应产生 supersede
    reg2 = _FakeKeyRegistry(key, memory_type="user_profile")
    monkeypatch.setattr(planner_mod, "get_memory_key_registry", lambda: reg2)
    actions2, _, _ = plan_batch("b2", [w, l], MaintenanceConfig(), _now())
    assert not [a for a in actions2 if a.action_type == "supersede"]


def test_merge_winner_tie_break_smallest_id(monkeypatch):
    """全平（含 observed_at）时 winner 取 memory_record_id 字典序最小者（设计 G 节 #50）。"""
    import aiive.memory.memory_maintenance_planner as planner_mod

    fixed = _now()
    a = _mk_frozen("bbb", "multi.key", "h", observed_at=fixed)
    b = _mk_frozen("aaa", "multi.key", "h", observed_at=fixed)
    c = _mk_frozen("ccc", "multi.key", "h", observed_at=fixed)
    winner = planner_mod._merge_winner([a, b, c])
    assert winner.memory_record_id == "aaa"


def test_lifecycle_supersede_creates_winner_and_supersedes_old(db):
    """supersede 动作：新建 winner 记录，旧记录全部 archived + superseded。"""
    now = _now()
    winner = _mk_record(db, canonical_key="sc.key", content="winner",
                        content_hash="hw", lifecycle_state=LifecycleState.ACTIVE.value)
    loser = _mk_record(db, canonical_key="sc.key", content="loser",
                       content_hash="hl", lifecycle_state=LifecycleState.ACTIVE.value)
    db.commit()

    svc = MemoryLifecycleService(db)
    action = _freeze_action(
        winner, "supersede", related=loser, reason="single_cardinality_superseded",
    )
    status = svc.apply_maintenance_action(action)
    db.commit()
    assert status == "applied"

    s = SessionLocal()
    try:
        fresh_w = s.get(MemoryRecord, winner.id)
        fresh_l = s.get(MemoryRecord, loser.id)
        assert fresh_w.lifecycle_state == LifecycleState.ARCHIVED.value
        assert fresh_w.validity_state == "superseded"
        assert fresh_l.lifecycle_state == LifecycleState.ARCHIVED.value
        assert fresh_l.validity_state == "superseded"
        # 新建的 winner 记录（非原 winner/loser id，保留 winner 内容）
        new_recs = s.query(MemoryRecord).filter(
            MemoryRecord.canonical_key == "sc.key",
            MemoryRecord.lifecycle_state == LifecycleState.ACTIVE.value,
            MemoryRecord.validity_state == "valid",
        ).all()
        assert len(new_recs) == 1
        new = new_recs[0]
        assert new.id not in (winner.id, loser.id)
        assert new.content == "winner"
        assert new.supersedes == winner.id
        assert fresh_w.superseded_by == new.id
        assert fresh_l.superseded_by == new.id
        # lineage：两条 SUPERSEDE 边指向新记录
        edges = s.query(MemoryLineage).filter(
            MemoryLineage.successor_id == new.id,
            MemoryLineage.operation == "supersede",
        ).count()
        assert edges == 2
    finally:
        s.close()
