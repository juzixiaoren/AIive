"""Phase 3: EpochManager 单元测试（测试矩阵 31/32/33/47/57/58/64/69）。"""
from __future__ import annotations

import uuid

from aiive.db.models import (
    CompactionInput,
    ContextSnapshot,
    Epoch,
    EpochCompactionInput,
    Segment,
)
from aiive.runtime.compaction import derive_unresolved_failures
from aiive.runtime.epoch_manager import (
    EpochManager,
    allocate_turn_sequence,
    peek_next_turn_sequence,
)
from tests._util import (
    add_event,
    add_turn,
    claim_job,
    new_epoch,
    new_segment,
    new_thread,
    new_ws,
)


def _max_turn_sequence(db, thread_id):
    from sqlalchemy import func
    from aiive.db.models import TurnRecord
    return db.query(func.max(TurnRecord.turn_sequence)).filter(
        TurnRecord.thread_id == thread_id).scalar() or 0


def test_57_successor_does_not_consume_turn_sequence(db):
    """创建 successor Segment 不消耗 Turn sequence（max(TurnRecord) 不变）。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    add_turn(db, thread.id, seg.id, 1)
    add_turn(db, thread.id, seg.id, 2)
    db.commit()

    before = _max_turn_sequence(db, thread.id)
    assert before == 2
    assert peek_next_turn_sequence(db, thread.id) == 3

    mgr = EpochManager()
    result = mgr.begin_segment_sealing(db, thread.id, seg.id)
    db.commit()

    # successor 以 peek 起点（只读不消费），max(TurnRecord) 仍为 2
    assert result.successor_segment_id is not None
    succ = db.get(Segment, result.successor_segment_id)
    assert succ.start_turn_sequence == 3
    assert peek_next_turn_sequence(db, thread.id) == 3
    assert allocate_turn_sequence(db, thread.id) == 3
    assert _max_turn_sequence(db, thread.id) == 2


def test_58_next_turn_allocates_to_successor(db):
    """下一个真实 Turn allocate 到 successor.start_turn_sequence 并归属新 Segment。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    add_turn(db, thread.id, seg.id, 1)
    add_turn(db, thread.id, seg.id, 2)
    db.commit()

    mgr = EpochManager()
    result = mgr.begin_segment_sealing(db, thread.id, seg.id)
    db.commit()
    succ = db.get(Segment, result.successor_segment_id)

    seq = allocate_turn_sequence(db, thread.id)
    new_turn = add_turn(db, thread.id, succ.id, seq)
    db.commit()

    assert seq == succ.start_turn_sequence == 3
    assert new_turn.segment_id == succ.id
    assert _max_turn_sequence(db, thread.id) == 3


def test_31_32_33_rollover_single_active_epoch_no_extra_open(db):
    """rollover 后仅一个 active Epoch；旧 Epoch 无多余 open Segment；新 Segment 用真实 seq。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id, epoch_no=1)
    seg = new_segment(db, epoch.id, thread.id, segment_no=1)
    add_turn(db, thread.id, seg.id, 1)
    add_turn(db, thread.id, seg.id, 2)
    db.commit()

    mgr = EpochManager()
    res = mgr.begin_epoch_sealing(db, thread.id)
    db.commit()

    active = db.query(Epoch).filter(
        Epoch.thread_id == thread.id, Epoch.status == "active").all()
    assert len(active) == 1
    assert res.new_epoch_id == active[0].id

    old_epoch = db.get(Epoch, epoch.id)
    assert old_epoch.status == "sealing"
    old_open = db.query(Segment).filter(
        Segment.epoch_id == old_epoch.id, Segment.status == "open").count()
    assert old_open == 0

    new_seg = db.get(Segment, res.new_segment_id)
    assert new_seg.start_turn_sequence == peek_next_turn_sequence(db, thread.id) == 3


def test_47_successor_uses_real_allocator(db):
    """successor Segment 使用真实 sequence allocator（peek 起点）。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    add_turn(db, thread.id, seg.id, 1)
    db.commit()

    mgr = EpochManager()
    result = mgr.begin_segment_sealing(db, thread.id, seg.id)
    db.commit()
    succ = db.get(Segment, result.successor_segment_id)
    assert succ.start_turn_sequence == peek_next_turn_sequence(db, thread.id)


def test_64_rollover_reuses_freeze_with_event_manifest(db):
    """Epoch rollover 复用 freeze_segment_for_sealing 生成含 Event hash/manifest 的 CompactionInput。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id, epoch_no=1)
    seg = new_segment(db, epoch.id, thread.id, segment_no=1)
    t1 = add_turn(db, thread.id, seg.id, 1, status="completed")
    add_event(db, thread.id, t1.turn_id, "user_message", {"content": "hi"}, 0)
    add_event(db, thread.id, t1.turn_id, "tool_call",
              {"name": "search", "tool_call_id": "tc1"}, 1)
    add_event(db, thread.id, t1.turn_id, "tool_result",
              {"name": "search", "tool_call_id": "tc1", "status": "completed"}, 2)
    db.commit()

    mgr = EpochManager()
    res = mgr.begin_epoch_sealing(db, thread.id)
    db.commit()

    ci = db.query(CompactionInput).filter(
        CompactionInput.segment_id == seg.id).first()
    assert ci is not None
    assert len(ci.event_manifest) == 3
    assert all("content_hash" in m and "turn_event_index" in m for m in ci.event_manifest)

    eci = db.query(EpochCompactionInput).filter(
        EpochCompactionInput.id == res.epoch_compaction_input_id).first()
    assert eci is not None
    assert eci.epoch_id == epoch.id


def test_69_failed_only_terminal_segment_sealable(db):
    """仅含 failed/cancelled/preempted 终态 Turn 的 Segment 可密封，并派生 unresolved_failures。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    failed_turn = add_turn(db, thread.id, seg.id, 1, status="failed")
    add_event(db, thread.id, failed_turn.turn_id, "tool_call",
              {"name": "boom", "tool_call_id": "tc_fail"}, 0)
    add_event(db, thread.id, failed_turn.turn_id, "tool_result",
              {"name": "boom", "tool_call_id": "tc_fail", "status": "error",
               "error": "kaboom"}, 1)
    db.commit()

    mgr = EpochManager()
    # 仅 failed Turn —— 非终态检查应放行
    reasons = mgr.check_sealable(db, thread.id)
    assert reasons == []

    result = mgr.begin_segment_sealing(db, thread.id, seg.id)
    db.commit()
    assert result.compaction_input_id is not None

    failures = derive_unresolved_failures(db, seg.id)
    assert len(failures) == 1
    assert failures[0]["tool_call_id"] == "tc_fail"


def _add_snapshot(db, thread_id, turn_sequence, retention):
    snap = ContextSnapshot(
        trace_id=str(uuid.uuid4()),
        thread_id=thread_id,
        stable_prefix_hash="",
        turn_sequence=turn_sequence,
        retention=retention,
    )
    db.add(snap)
    db.flush()
    return snap


def test_sealing_promotes_current_snapshot_to_audit(db):
    """Segment 密封时，current 快照晋升为 audit（Phase 1 §14.5）。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    add_turn(db, thread.id, seg.id, 1)
    add_turn(db, thread.id, seg.id, 2)
    _add_snapshot(db, thread.id, 2, "current")
    db.commit()

    mgr = EpochManager()
    mgr.begin_segment_sealing(db, thread.id, seg.id)
    db.commit()

    snaps = db.query(ContextSnapshot).filter(
        ContextSnapshot.thread_id == thread.id,
    ).all()
    audit = [s for s in snaps if s.retention == "audit"]
    current = [s for s in snaps if s.retention == "current"]
    assert len(audit) == 1
    assert len(current) == 0


def test_audit_snapshots_capped_at_max(db):
    """审计快照上限：密封时超过 MAX_AUDIT_SNAPSHOTS 的最旧快照被裁剪。"""
    from aiive.runtime.epoch_manager import MAX_AUDIT_SNAPSHOTS
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    add_turn(db, thread.id, seg.id, 1)
    for i in range(MAX_AUDIT_SNAPSHOTS):
        _add_snapshot(db, thread.id, i + 1, "audit")
    _add_snapshot(db, thread.id, MAX_AUDIT_SNAPSHOTS + 1, "current")
    db.commit()

    mgr = EpochManager()
    mgr.begin_segment_sealing(db, thread.id, seg.id)
    db.commit()

    audit = db.query(ContextSnapshot).filter(
        ContextSnapshot.thread_id == thread.id,
        ContextSnapshot.retention == "audit",
    ).all()
    assert len(audit) == MAX_AUDIT_SNAPSHOTS
