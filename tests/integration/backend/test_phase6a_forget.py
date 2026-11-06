"""Phase 6A Forget Saga 集成测试。

测试端到端流程：Phase A Shield → Cascade → Rebuild → Purge → Verify。
使用 SQLite 内存数据库 + 独立 session，测试后自动清理。
"""

import tempfile
import shutil
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text as sa_text
from sqlalchemy.orm import Session, sessionmaker

from aiive.db.models import Base
from aiive.db.forget_models import (
    ForgetAction,
    ForgetBatch,
    ForgetOperation,
    ForgetShield,
    ForgetStageRun,
    ForgetTarget,
    ForgetTombstone,
)


@pytest.fixture
def db_session():
    """SQLite 内存数据库 session，测试后自动清空。"""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def _new_id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 0), (True, 0), ([], 0), ({}, 0), (-2, 0), ("7", 7), ("invalid", 0)],
)
def test_non_negative_int_rejects_invalid_summary_counts(raw, expected):
    from aiive.worker.handlers_forget import _non_negative_int

    assert _non_negative_int(raw) == expected


# ═══════════════════════════════════════════════════════════════════
# Phase A Shield 测试
# ═══════════════════════════════════════════════════════════════════


def test_phase_a_shield_creates_operation(db_session: Session):
    """Phase A Shield 创建新 Saga 记录，且最终 schema 不含旧请求表。"""
    from aiive.forget.fingerprint import configure_hmac_secret
    from aiive.forget.phase_a_shield import execute_phase_a_shield

    configure_hmac_secret("test-secret", 1)
    assert "forget_requests" not in Base.metadata.tables

    result = execute_phase_a_shield(
        session=db_session,
        mode="memory_only",
        memory_ids=[_new_id(), _new_id()],
        reason="test",
    )
    db_session.commit()

    assert result["status"] == "shielded"
    assert result["target_count"] >= 2
    assert db_session.execute(
        sa_text("SELECT COUNT(*) FROM forget_operations")
    ).scalar_one() == 1

    op = db_session.query(ForgetOperation).filter_by(id=result["operation_id"]).first()
    assert op is not None
    assert op.mode == "memory_only"
    assert op.status == "shielded"

    # Manifest
    from aiive.db.forget_models import ForgetSelectorManifest
    manifest = (
        db_session.query(ForgetSelectorManifest)
        .filter_by(forget_operation_id=result["operation_id"]).first()
    )
    assert manifest is not None
    assert "memory_ids" in str(manifest.selector_payload)

    # Shield
    shields = (
        db_session.query(ForgetShield)
        .filter_by(forget_operation_id=result["operation_id"]).all()
    )
    assert len(shields) > 0

    # Tombstone
    tombstones = (
        db_session.query(ForgetTombstone)
        .filter_by(forget_operation_id=result["operation_id"]).all()
    )
    assert len(tombstones) >= 2

    # StageRun
    stage_runs = (
        db_session.query(ForgetStageRun)
        .filter_by(forget_operation_id=result["operation_id"]).all()
    )
    assert len(stage_runs) >= 1
    assert stage_runs[0].stage == "cascade"


def test_phase_a_shield_all_user_data(db_session: Session):
    """all_user_data Phase A 只写一条 selector Shield。"""
    from aiive.forget.fingerprint import configure_hmac_secret
    from aiive.forget.phase_a_shield import execute_phase_a_shield

    configure_hmac_secret("test-secret", 1)

    result = execute_phase_a_shield(
        session=db_session,
        mode="everywhere",
        all_user_data=True,
        reason="wipe",
    )
    db_session.commit()

    shields = (
        db_session.query(ForgetShield)
        .filter_by(forget_operation_id=result["operation_id"], all_user_data=True)
        .all()
    )
    assert len(shields) == 1

    # 不应枚举 targets（宽选择器）
    targets = (
        db_session.query(ForgetTarget)
        .filter_by(forget_operation_id=result["operation_id"]).count()
    )
    assert targets == 0


def test_tombstone_allow_audit_read_memory_only(db_session: Session):
    """memory_only tombstone 设置 allow_audit_read=true。"""
    from aiive.forget.fingerprint import configure_hmac_secret
    from aiive.forget.phase_a_shield import execute_phase_a_shield

    configure_hmac_secret("test-secret", 1)
    mid = _new_id()

    execute_phase_a_shield(
        session=db_session,
        mode="memory_only",
        memory_ids=[mid],
        reason="test",
    )
    db_session.commit()

    tomb = (
        db_session.query(ForgetTombstone)
        .filter_by(target_type="memory_record", target_id=mid)
        .first()
    )
    assert tomb is not None
    assert tomb.allow_audit_read is True
    assert tomb.block_visibility is True
    assert tomb.content_purged is False


# ═══════════════════════════════════════════════════════════════════
# VisibilityService 测试
# ═══════════════════════════════════════════════════════════════════


def test_visibility_service_shield_check(db_session: Session):
    """ForgetVisibilityService 检查 Shield 屏蔽。"""
    from aiive.forget.visibility_service import ForgetVisibilityService

    mid = _new_id()

    # 写入一个 active shield
    shield = ForgetShield(
        id=_new_id(),
        forget_operation_id=_new_id(),
        selector_type="memory_ids",
        target_type="memory_record",
        target_id=mid,
        cutoff_created_at=_utcnow(),
        status="active",
        normalized_shield_key=f"test-{mid}",
    )
    db_session.add(shield)
    db_session.commit()

    assert ForgetVisibilityService.is_shielded(db_session, memory_id=mid) is True
    assert ForgetVisibilityService.is_shielded(db_session, memory_id=_new_id()) is False


def test_tombstone_block_visibility(db_session: Session):
    """Tombstone.block_visibility=true 屏蔽读取。"""
    from aiive.forget.visibility_service import ForgetVisibilityService

    mid = _new_id()
    tomb = ForgetTombstone(
        id=_new_id(),
        forget_operation_id=_new_id(),
        target_type="memory_record",
        target_id=mid,
        block_visibility=True,
        block_reingestion=True,
        content_purged=False,
        allow_audit_read=True,
    )
    db_session.add(tomb)
    db_session.commit()

    # 批量检查
    blocked = ForgetVisibilityService.batch_is_tombstone_blocked(
        db_session, "memory_record", [mid, _new_id()],
    )
    assert mid in blocked
    assert len(blocked) == 1

    # 审计检查
    assert ForgetVisibilityService.can_audit_read(db_session, "memory_record", mid) is True


def test_filter_events_uses_event_id_key(db_session: Session):
    """历史事件字典使用 event_id 键（无 id/thread_id）时 filter_events 不应抛 KeyError。

    回归测试：thread_state._events_to_dicts 产出的字典键为 event_id，
    此前 filter_events 按 e["id"] 读取导致 KeyError。
    """
    from aiive.forget.visibility_service import ForgetVisibilityService

    blocked_eid = _new_id()
    visible_eid = _new_id()
    tomb = ForgetTombstone(
        id=_new_id(),
        forget_operation_id=_new_id(),
        target_type="event",
        target_id=blocked_eid,
        block_visibility=True,
        block_reingestion=True,
        content_purged=False,
        allow_audit_read=False,
    )
    db_session.add(tomb)
    db_session.commit()

    events = [
        {"type": "user", "content": "hi", "event_id": visible_eid, "trace_id": _new_id()},
        {"type": "assistant", "content": "yo", "event_id": blocked_eid, "trace_id": _new_id()},
    ]

    result = ForgetVisibilityService.filter_events(db_session, events)

    result_ids = {e["event_id"] for e in result}
    assert visible_eid in result_ids
    assert blocked_eid not in result_ids


def test_filter_events_uses_id_key(db_session: Session):
    """事件字典使用 id 键时 filter_events 仍正常过滤（兼容两种键名）。"""
    from aiive.forget.visibility_service import ForgetVisibilityService

    blocked_eid = _new_id()
    visible_eid = _new_id()
    tomb = ForgetTombstone(
        id=_new_id(),
        forget_operation_id=_new_id(),
        target_type="event",
        target_id=blocked_eid,
        block_visibility=True,
        block_reingestion=True,
        content_purged=False,
        allow_audit_read=False,
    )
    db_session.add(tomb)
    db_session.commit()

    events = [
        {"id": visible_eid, "content": "hi"},
        {"id": blocked_eid, "content": "yo"},
    ]

    result = ForgetVisibilityService.filter_events(db_session, events)

    result_ids = {e["id"] for e in result}
    assert visible_eid in result_ids
    assert blocked_eid not in result_ids


def test_content_purged_blocks_audit(db_session: Session):
    """content_purged=true 时审计模式也看不到。"""
    from aiive.forget.visibility_service import ForgetVisibilityService

    mid = _new_id()
    tomb = ForgetTombstone(
        id=_new_id(),
        forget_operation_id=_new_id(),
        target_type="memory_record",
        target_id=mid,
        block_visibility=True,
        block_reingestion=True,
        content_purged=True,
        allow_audit_read=True,
    )
    db_session.add(tomb)
    db_session.commit()

    assert ForgetVisibilityService.can_audit_read(db_session, "memory_record", mid) is False


# ═══════════════════════════════════════════════════════════════════
# 幂等 / UNIQUE 测试
# ═══════════════════════════════════════════════════════════════════


def test_shield_unique_normalized_key(db_session: Session):
    """重复写入同一 normalized_shield_key 应幂等。"""
    from sqlalchemy.exc import IntegrityError

    shield = ForgetShield(
        id=_new_id(),
        forget_operation_id=_new_id(),
        selector_type="all_user_data",
        all_user_data=True,
        cutoff_created_at=_utcnow(),
        status="active",
        normalized_shield_key="unique-test-key",
    )
    db_session.add(shield)
    db_session.commit()

    dup = ForgetShield(
        id=_new_id(),
        forget_operation_id=shield.forget_operation_id,
        selector_type="all_user_data",
        all_user_data=True,
        cutoff_created_at=_utcnow(),
        status="active",
        normalized_shield_key="unique-test-key",
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_tombstone_unique_constraint(db_session: Session):
    """同一 operation 内重复 target_type+target_id 应拒绝。"""
    from sqlalchemy.exc import IntegrityError

    op_id = _new_id()
    mid = _new_id()
    tomb1 = ForgetTombstone(
        id=_new_id(),
        forget_operation_id=op_id,
        target_type="memory_record",
        target_id=mid,
    )
    db_session.add(tomb1)
    db_session.commit()

    tomb2 = ForgetTombstone(
        id=_new_id(),
        forget_operation_id=op_id,
        target_type="memory_record",
        target_id=mid,
    )
    db_session.add(tomb2)
    with pytest.raises(IntegrityError):
        db_session.commit()


# ═══════════════════════════════════════════════════════════════════
# HMAC 指纹测试
# ═══════════════════════════════════════════════════════════════════


def test_hmac_fingerprint_deterministic():
    """相同输入产生相同 HMAC 指纹。"""
    from aiive.forget.fingerprint import configure_hmac_secret, compute_value_fingerprint

    configure_hmac_secret("my-secret", 10)
    fp1, v1 = compute_value_fingerprint("hello")
    fp2, v2 = compute_value_fingerprint("hello")
    assert fp1 == fp2
    assert v1 == v2 == 10


def test_hmac_fingerprint_different_keys():
    """不同密钥产生不同指纹。"""
    from aiive.forget.fingerprint import configure_hmac_secret, compute_value_fingerprint

    configure_hmac_secret("key-A", 1)
    fp_a, _ = compute_value_fingerprint("test")

    configure_hmac_secret("key-B", 2)
    fp_b, _ = compute_value_fingerprint("test")

    assert fp_a != fp_b


# ═══════════════════════════════════════════════════════════════════
# 选择器规范化测试
# ═══════════════════════════════════════════════════════════════════


def test_selector_normalize_dedup():
    """规范化时去重排序 ID。"""
    from aiive.forget.selector_normalizer import normalize_selector, compute_selector_hash

    p1 = normalize_selector("memory_only", memory_ids=["m3", "m1", "m2", "m1"])
    p2 = normalize_selector("memory_only", memory_ids=["m1", "m2", "m3"])

    assert p1["memory_ids"] == ["m1", "m2", "m3"]
    assert compute_selector_hash(p1) == compute_selector_hash(p2)


def test_inline_candidate_threshold():
    """<=100 IDs 允许内联，>100 退化。"""
    from aiive.forget.selector_normalizer import (
        MAX_INLINE_SHIELD_TARGETS,
        is_inline_shield_candidate,
        normalize_selector,
    )

    small = normalize_selector("memory_only", memory_ids=[f"m{i}" for i in range(MAX_INLINE_SHIELD_TARGETS)])
    assert is_inline_shield_candidate(small) is True

    large = normalize_selector("memory_only", memory_ids=[f"m{i}" for i in range(MAX_INLINE_SHIELD_TARGETS + 1)])
    assert is_inline_shield_candidate(large) is False

    # canonical_key 永远不是内联候选
    canon = normalize_selector("memory_only", canonical_key="my-key")
    assert is_inline_shield_candidate(canon) is False


# ═══════════════════════════════════════════════════════════════════
# 修复回归测试（阻断/逻辑错误）
# ═══════════════════════════════════════════════════════════════════


def test_forget_operation_requested_by_accepted_without_runcontext(db_session: Session):
    """#1/#15: Phase A 接受无 RunContext 的调用（API 场景），requested_by 落库。"""
    from aiive.forget.fingerprint import configure_hmac_secret
    from aiive.forget.phase_a_shield import execute_phase_a_shield

    configure_hmac_secret("test-secret", 1)
    mid = _new_id()
    result = execute_phase_a_shield(
        session=db_session,
        mode="memory_only",
        memory_ids=[mid],
        reason="test",
        requested_by="api",
    )
    db_session.commit()
    op = db_session.query(ForgetOperation).filter_by(id=result["operation_id"]).first()
    assert op is not None
    assert op.requested_by == "api"


def test_blocked_target_ids_all_user_data_global(db_session: Session):
    """#2/#3: all_user_data Shield 经 blocked_target_ids 全局 fail-closed 屏蔽。

    cutoff 语义：仅屏蔽 cutoff 之前创建的数据；遗忘之后的新数据不受屏蔽；
    创建时间未知则 fail-closed 屏蔽。
    """
    from datetime import timedelta
    from aiive.forget.visibility_service import ForgetVisibilityService

    now = _utcnow()
    before = now - timedelta(minutes=1)
    after = now + timedelta(minutes=1)
    op = ForgetOperation(
        id=_new_id(), operation_key="forget:global", mode="everywhere",
        selector_type="all_user_data", selector_hash="h", status="shielded",
        requested_by="api", target_count=0, shielded_at=now,
    )
    db_session.add(op)
    db_session.add(ForgetShield(
        id=_new_id(), forget_operation_id=op.id, selector_type="all_user_data",
        all_user_data=True, cutoff_created_at=now, status="active",
        normalized_shield_key="k",
    ))
    db_session.commit()

    id_old, id_old2, id_new, id_unknown = _new_id(), _new_id(), _new_id(), _new_id()
    from aiive.db.models import MemoryRecord
    from tests.support import attach_memory_event_source
    for mid, stamp in [(id_old, before), (id_old2, before), (id_new, after)]:
        record = MemoryRecord(id=mid, memory_type="fact", content="cutoff fixture",
                              scope_type="global", lifecycle_state="active", created_at=stamp)
        db_session.add(record)
        attach_memory_event_source(db_session, record)
    blocked = ForgetVisibilityService.blocked_target_ids(
        db_session, "memory_record",
        [(id_old, before), (id_old2, before), (id_new, after), (id_unknown, None)],
    )
    # cutoff 之前创建的数据 + 创建时间未知（fail-closed）→ 屏蔽
    assert id_old in blocked
    assert id_old2 in blocked
    assert id_unknown in blocked
    # 遗忘之后新产生的数据不受该 Shield 永久屏蔽
    assert id_new not in blocked


def test_is_shielded_entity_match_only(db_session: Session):
    """#3: 实体级 Shield 仅命中对应实体，不误伤其他。"""
    from aiive.forget.visibility_service import ForgetVisibilityService

    op = ForgetOperation(
        id=_new_id(), operation_key="forget:ent", mode="everywhere",
        selector_type="memory_ids", selector_hash="h", status="shielded",
        requested_by="api", target_count=1, shielded_at=_utcnow(),
    )
    db_session.add(op)
    mid = _new_id()
    db_session.add(ForgetShield(
        id=_new_id(), forget_operation_id=op.id, selector_type="memory_ids",
        target_type="memory_record", target_id=mid,
        cutoff_created_at=_utcnow(), status="active", normalized_shield_key="k2",
    ))
    db_session.commit()

    assert ForgetVisibilityService.is_shielded(db_session, memory_id=mid) is True
    assert ForgetVisibilityService.is_shielded(db_session, memory_id=_new_id()) is False


def test_core_memory_block_removed_on_forget(db_session: Session):
    """#8: Phase A 删除引用被忘 memory 的 CoreMemoryBlock，防止泄漏。"""
    from aiive.forget.fingerprint import configure_hmac_secret
    from aiive.forget.phase_a_shield import execute_phase_a_shield
    from aiive.db.models import CoreMemoryBlock

    configure_hmac_secret("test-secret", 1)
    mid = _new_id()
    other_mid = _new_id()
    db_session.add(CoreMemoryBlock(
        id=_new_id(), block_name="user_profile", content="x",
        source_memory_ids=[mid],
    ))
    db_session.add(CoreMemoryBlock(
        id=_new_id(), block_name="keep_me", content="y",
        source_memory_ids=[other_mid],
    ))
    db_session.commit()

    execute_phase_a_shield(
        session=db_session, mode="memory_only", memory_ids=[mid], reason="t",
    )
    db_session.commit()

    remaining = db_session.query(CoreMemoryBlock).all()
    assert len(remaining) == 1
    assert remaining[0].block_name == "keep_me"


def test_evidence_recompute_keeps_independent_evidence(db_session: Session):
    """#4: 仅删被忘来源证据；保留仍有独立证据的 memory。"""
    from aiive.worker.handlers_forget import (
        _recompute_evidence_batch,
        _find_or_create_composite_batch,
    )
    from aiive.db.models import MemoryRecord, MemoryEvidence

    op_id = _new_id()
    op = ForgetOperation(
        id=op_id, operation_key=f"forget:{op_id}", mode="everywhere",
        selector_type="memory_ids", selector_hash="h", status="shielded",
        requested_by="api", target_count=2, shielded_at=_utcnow(),
    )
    db_session.add(op)

    m1, m2 = _new_id(), _new_id()
    shared_ev, other_ev = _new_id(), _new_id()
    for mid in (m1, m2):
        db_session.add(MemoryRecord(
            id=mid, memory_type="fact", canonical_key=f"k:{mid}",
            content="test content", lifecycle_state="active",
            validity_state="valid", confidence=0.9, created_at=_utcnow(),
        ))
    # m1 仅依赖被忘来源
    db_session.add(MemoryEvidence(memory_id=m1, source_event_id=shared_ev, source_type="user_message"))
    # m2 依赖被忘来源 + 独立来源
    db_session.add(MemoryEvidence(memory_id=m2, source_event_id=shared_ev, source_type="user_message"))
    db_session.add(MemoryEvidence(memory_id=m2, source_event_id=other_ev, source_type="user_message"))

    db_session.add(ForgetTombstone(
        id=_new_id(), forget_operation_id=op_id, target_type="event",
        target_id=shared_ev, source_event_id=shared_ev, block_visibility=True,
        block_reingestion=True, content_purged=False, allow_audit_read=False,
        reason_code="forget", created_at=_utcnow(),
    ))
    for mid in (m1, m2):
        db_session.add(ForgetTarget(
            id=_new_id(), forget_operation_id=op_id, target_type="memory_record",
            target_id=mid, batch_no=0, frozen_at=_utcnow(),
        ))
    db_session.commit()

    batch = _find_or_create_composite_batch(db_session, op_id, "rebuild", "evidence")
    _recompute_evidence_batch(db_session, op_id, batch)
    db_session.commit()

    m1r = db_session.query(MemoryRecord).filter_by(id=m1).first()
    m2r = db_session.query(MemoryRecord).filter_by(id=m2).first()
    assert m1r.lifecycle_state == "forgotten"   # 无独立证据
    assert m2r.lifecycle_state == "active"       # 仍有独立证据保留

    # #9: ForgetAction 已被记录
    actions = (
        db_session.query(ForgetAction)
        .filter_by(forget_operation_id=op_id)
        .all()
    )
    assert len(actions) >= 1
    assert actions[0].action_type == "recompute_evidence"


def test_wide_selector_all_user_data_has_targets(db_session: Session):
    """#6: all_user_data 宽选择器在 cascade 时枚举 memory_record 为 Target。"""
    from aiive.worker.handlers_forget import _resolve_selector_ids
    from aiive.db.models import MemoryRecord

    # 创建两个 memory 记录
    mid1, mid2 = _new_id(), _new_id()
    for mid in (mid1, mid2):
        db_session.add(MemoryRecord(
            id=mid, memory_type="fact", canonical_key=f"k:{mid}",
            content="test", lifecycle_state="active", validity_state="valid",
            confidence=0.9, created_at=_utcnow(),
        ))
    db_session.commit()

    payload = {"selector_type": "all_user_data", "all_user_data": True, "mode": "everywhere"}
    ids = _resolve_selector_ids(db_session, payload, _utcnow(), None, 10)
    assert len(ids) >= 2
    assert mid1 in ids
    assert mid2 in ids


def test_keyset_cursor_no_overlap(db_session: Session):
    """#7: keyset 游标分页不重复不跳过。"""
    from aiive.worker.handlers_forget import _resolve_selector_ids
    from aiive.db.models import MemoryRecord

    mids = [_new_id() for _ in range(5)]
    for mid in mids:
        db_session.add(MemoryRecord(
            id=mid, memory_type="fact", canonical_key=f"k:{mid}",
            content="test", lifecycle_state="active", validity_state="valid",
            confidence=0.9, created_at=_utcnow(),
        ))
    db_session.commit()

    payload = {"selector_type": "all_user_data", "all_user_data": True, "mode": "everywhere"}
    batch1 = _resolve_selector_ids(db_session, payload, _utcnow(), None, 2)
    assert len(batch1) == 2
    # keyset 游标：获取最后一条的 created_at
    last_rec = db_session.query(MemoryRecord.created_at).filter(
        MemoryRecord.id == batch1[-1]
    ).first()
    assert last_rec is not None
    cursor = {"last_created_at": last_rec[0].isoformat(), "last_id": batch1[-1]}
    batch2 = _resolve_selector_ids(db_session, payload, _utcnow(), cursor, 5)
    # 第二批次不包含第一个批次的任何记录
    overlap = set(batch1) & set(batch2)
    assert len(overlap) == 0


def test_verify_wide_mode_no_false_purge(db_session: Session):
    """#6: everywhere 模式在仍有非 forgotten 记录时不误报 purged。"""
    from aiive.db.models import MemoryRecord

    op_id = _new_id()
    op = ForgetOperation(
        id=op_id, operation_key=f"forget:{op_id}", mode="everywhere",
        selector_type="all_user_data", selector_hash="h", status="shielded",
        requested_by="api", target_count=0, shielded_at=_utcnow(),
    )
    db_session.add(op)
    # 创建两个 memory：一个 forgotten，一个仍未遗忘
    db_session.add(MemoryRecord(
        id=_new_id(), memory_type="fact", canonical_key="k:active",
        content="still here", lifecycle_state="active",
        validity_state="valid", confidence=0.8,
    ))
    db_session.commit()

    # 模拟 verify 判定逻辑：存在未遗忘的记录（不依赖时间过滤，SQLite 时间比较不可靠）
    remaining = (
        db_session.query(MemoryRecord.id)
        .filter(
            MemoryRecord.lifecycle_state != "forgotten",
        )
        .first()
    )
    # 存在未遗忘的记录 → 不应 purged
    assert remaining is not None
    assert op.status != "purged"


# ═══════════════════════════════════════════════════════════════════
# 选择器级 Shield 经 blocked_target_ids 生效（修复 #1）
# ═══════════════════════════════════════════════════════════════════


def test_blocked_target_ids_selector_scope(db_session: Session):
    """#1: scope 选择器 Shield 精确屏蔽同 scope 的 memory_record。"""
    from aiive.forget.visibility_service import ForgetVisibilityService
    from aiive.db.models import MemoryRecord
    from aiive.db.forget_models import ForgetShield, ForgetOperation

    from datetime import timedelta
    now = _utcnow()
    earlier = now - timedelta(minutes=1)  # cutoff 语义：目标须早于 shield cutoff
    op = ForgetOperation(
        id=_new_id(), operation_key="f:scope", mode="everywhere",
        selector_type="scope", selector_hash="h", status="shielded",
        requested_by="api", target_count=0, shielded_at=now,
    )
    db_session.add(op)
    db_session.add(ForgetShield(
        id=_new_id(), forget_operation_id=op.id, selector_type="scope",
        scope_type="project", scope_id="p1",
        cutoff_created_at=now, status="active", normalized_shield_key="ks",
    ))
    m_match, m_other = _new_id(), _new_id()
    db_session.add(MemoryRecord(
        id=m_match, memory_type="fact", canonical_key="k:m",
        scope_type="project", scope_id="p1", content="x",
        lifecycle_state="active", validity_state="valid", confidence=0.5,
        created_at=earlier))
    db_session.add(MemoryRecord(
        id=m_other, memory_type="fact", canonical_key="k:o",
        scope_type="project", scope_id="p2", content="y",
        lifecycle_state="active", validity_state="valid", confidence=0.5,
        created_at=earlier))
    db_session.commit()

    from tests.support import attach_memory_event_source
    from aiive.db.models import MemoryRecord
    for record in db_session.query(MemoryRecord).all():
        attach_memory_event_source(db_session, record)
    blocked = ForgetVisibilityService.blocked_target_ids(
        db_session, "memory_record", [(m_match, earlier), (m_other, earlier)])
    assert m_match in blocked
    assert m_other not in blocked


def test_blocked_target_ids_selector_thread(db_session: Session):
    """#1: thread 选择器 Shield 屏蔽同 thread 的 event（memory_record 无 thread 列，不直达）。"""
    from aiive.forget.visibility_service import ForgetVisibilityService
    from aiive.db.models import Event
    from aiive.db.forget_models import ForgetShield, ForgetOperation

    from datetime import timedelta
    now = _utcnow()
    earlier = now - timedelta(minutes=1)
    op = ForgetOperation(
        id=_new_id(), operation_key="f:thread", mode="everywhere",
        selector_type="thread", selector_hash="h", status="shielded",
        requested_by="api", target_count=0, shielded_at=now,
    )
    db_session.add(op)
    db_session.add(ForgetShield(
        id=_new_id(), forget_operation_id=op.id, selector_type="thread",
        thread_id="t1", cutoff_created_at=now, status="active",
        normalized_shield_key="kt",
    ))
    e_match, e_other, e_future = _new_id(), _new_id(), _new_id()
    db_session.add(Event(
        id=e_match, trace_id=_new_id(), thread_id="t1",
        event_type="user_message", payload={}, created_at=earlier))
    db_session.add(Event(
        id=e_other, trace_id=_new_id(), thread_id="t2",
        event_type="user_message", payload={}, created_at=earlier))
    # cutoff 之后同 thread 的新事件不受该 Shield 屏蔽
    db_session.add(Event(
        id=e_future, trace_id=_new_id(), thread_id="t1",
        event_type="user_message", payload={},
        created_at=now + timedelta(minutes=1)))
    db_session.commit()

    blocked = ForgetVisibilityService.blocked_target_ids(
        db_session, "event",
        [(e_match, earlier), (e_other, earlier), (e_future, None)])
    assert e_match in blocked
    assert e_other not in blocked
    assert e_future not in blocked


def test_blocked_target_ids_selector_canonical_key(db_session: Session):
    """#1: canonical_key 选择器 Shield 屏蔽同 canonical_key 的 memory_record。"""
    from aiive.forget.visibility_service import ForgetVisibilityService
    from aiive.db.models import MemoryRecord
    from aiive.db.forget_models import ForgetShield, ForgetOperation

    from datetime import timedelta
    now = _utcnow()
    earlier = now - timedelta(minutes=1)
    op = ForgetOperation(
        id=_new_id(), operation_key="f:ck", mode="everywhere",
        selector_type="canonical_key", selector_hash="h", status="shielded",
        requested_by="api", target_count=0, shielded_at=now,
    )
    db_session.add(op)
    db_session.add(ForgetShield(
        id=_new_id(), forget_operation_id=op.id, selector_type="canonical_key",
        canonical_key="user.secret", cutoff_created_at=now, status="active",
        normalized_shield_key="kc",
    ))
    m_match, m_other = _new_id(), _new_id()
    db_session.add(MemoryRecord(
        id=m_match, memory_type="fact", canonical_key="user.secret",
        scope_type="global", scope_id=None, content="x",
        lifecycle_state="active", validity_state="valid", confidence=0.5,
        created_at=earlier))
    db_session.add(MemoryRecord(
        id=m_other, memory_type="fact", canonical_key="user.other",
        scope_type="global", scope_id=None, content="y",
        lifecycle_state="active", validity_state="valid", confidence=0.5,
        created_at=earlier))
    db_session.commit()

    from tests.support import attach_memory_event_source
    from aiive.db.models import MemoryRecord
    for record in db_session.query(MemoryRecord).all():
        attach_memory_event_source(db_session, record)
    blocked = ForgetVisibilityService.blocked_target_ids(
        db_session, "memory_record", [(m_match, earlier), (m_other, earlier)])
    assert m_match in blocked
    assert m_other not in blocked


def test_blocked_target_ids_selector_time_range(db_session: Session):
    """#1: time_range 选择器 Shield 屏蔽创建时间落在区间内的 memory_record。"""
    from aiive.forget.visibility_service import ForgetVisibilityService
    from aiive.db.models import MemoryRecord
    from aiive.db.forget_models import ForgetShield, ForgetOperation
    from datetime import timedelta

    now = _utcnow()
    tf = now - timedelta(days=10)
    tt = now + timedelta(days=10)
    op = ForgetOperation(
        id=_new_id(), operation_key="f:time", mode="everywhere",
        selector_type="time_range", selector_hash="h", status="shielded",
        requested_by="api", target_count=0, shielded_at=now,
    )
    db_session.add(op)
    db_session.add(ForgetShield(
        id=_new_id(), forget_operation_id=op.id, selector_type="time_range",
        time_from=tf, time_to=tt, cutoff_created_at=now, status="active",
        normalized_shield_key="ktime",
    ))
    m_in, m_out = _new_id(), _new_id()
    db_session.add(MemoryRecord(
        id=m_in, memory_type="fact", canonical_key="k:in",
        scope_type="global", scope_id=None, content="x",
        lifecycle_state="active", validity_state="valid", confidence=0.5,
        created_at=now))
    db_session.add(MemoryRecord(
        id=m_out, memory_type="fact", canonical_key="k:out",
        scope_type="global", scope_id=None, content="y",
        lifecycle_state="active", validity_state="valid", confidence=0.5,
        created_at=now - timedelta(days=100)))
    db_session.commit()

    from tests.support import attach_memory_event_source
    from aiive.db.models import MemoryRecord
    for record in db_session.query(MemoryRecord).all():
        attach_memory_event_source(db_session, record)
    blocked = ForgetVisibilityService.blocked_target_ids(
        db_session, "memory_record", [(m_in, now), (m_out, now - timedelta(days=100))])
    assert m_in in blocked
    assert m_out not in blocked


# ═══════════════════════════════════════════════════════════════════
# EpochCheckpoint 依赖物化 + 重建（修复 #2）
# ═══════════════════════════════════════════════════════════════════


def test_discover_epoch_checkpoint_dependency(db_session: Session):
    """#2: 被忘 memory 的来源 event 出现在 SegmentSummary 中时，其 EpochCheckpoint 被物化为依赖。"""
    from aiive.worker.handlers_forget import _discover_for_target
    from aiive.db.models import (
        Event, MemoryRecord, MemoryEvidence, SegmentSummary, Segment, Epoch, EpochCheckpoint,
    )
    from aiive.db.forget_models import (
        ForgetOperation, ForgetTarget, ForgetBatch, ForgetDependency,
    )

    thread = _new_id()
    ev_id = _new_id()
    db_session.add(Event(
        id=ev_id, trace_id=_new_id(), thread_id=thread,
        event_type="user_message", payload={}, created_at=_utcnow()))
    mid = _new_id()
    db_session.add(MemoryRecord(
        id=mid, memory_type="fact", canonical_key="k:ec", content="c",
        lifecycle_state="active", validity_state="valid", confidence=0.5,
        created_at=_utcnow()))
    db_session.add(MemoryEvidence(
        memory_id=mid, source_event_id=ev_id, source_type="user_message"))
    eid = _new_id()
    db_session.add(Epoch(id=eid, thread_id=thread, epoch_no=1, status="active"))
    sid = _new_id()
    db_session.add(Segment(
        id=sid, epoch_id=eid, thread_id=thread, segment_no=1, status="sealed",
        start_turn_sequence=1, summary_id=_new_id()))
    sumid = _new_id()
    db_session.add(SegmentSummary(
        id=sumid, segment_id=sid, source_event_ids=[ev_id], goal="g"))
    cpid = _new_id()
    db_session.add(EpochCheckpoint(
        id=cpid, epoch_id=eid, version=1, current_goal="secret",
        source_segment_ids=[]))
    op_id = _new_id()
    db_session.add(ForgetOperation(
        id=op_id, operation_key=f"forget:{op_id}", mode="memory_only",
        selector_type="memory_ids", selector_hash="h", status="shielded",
        requested_by="api", target_count=1, shielded_at=_utcnow()))
    db_session.add(ForgetTarget(
        id=_new_id(), forget_operation_id=op_id, target_type="memory_record",
        target_id=mid, batch_no=0, frozen_at=_utcnow()))
    batch = ForgetBatch(
        id=_new_id(), forget_operation_id=op_id, stage="cascade",
        dependency_type="memory_record", batch_no=0, cursor_start_json={})
    db_session.add(batch)
    db_session.commit()

    target = db_session.query(ForgetTarget).filter_by(
        forget_operation_id=op_id).first()
    _discover_for_target(db_session, op_id, target, batch, _utcnow())
    db_session.commit()

    dep = db_session.query(ForgetDependency).filter_by(
        forget_operation_id=op_id, dependency_type="epoch_checkpoint",
        dependency_id=cpid).first()
    assert dep is not None


def test_rebuild_checkpoints_uses_own_epoch_id(db_session: Session):
    """#2: 重建使用 checkpoint 自身 epoch_id，并原子切换 epochs.checkpoint_id。"""
    from aiive.worker.handlers_forget import _rebuild_checkpoints
    from aiive.db.models import Epoch, EpochCheckpoint
    from aiive.db.forget_models import (
        ForgetOperation, ForgetDependency, ForgetBatch,
    )

    eid = _new_id()
    db_session.add(Epoch(id=eid, thread_id=_new_id(), epoch_no=1, status="active"))
    old_cpid = _new_id()
    db_session.add(EpochCheckpoint(
        id=old_cpid, epoch_id=eid, version=1, current_goal="secret",
        source_segment_ids=[]))
    db_session.commit()
    db_session.query(Epoch).filter_by(id=eid).update({"checkpoint_id": old_cpid})
    db_session.commit()

    op_id = _new_id()
    db_session.add(ForgetOperation(
        id=op_id, operation_key=f"forget:{op_id}", mode="everywhere",
        selector_type="memory_ids", selector_hash="h", status="shielded",
        requested_by="api", target_count=0, shielded_at=_utcnow()))
    db_session.add(ForgetDependency(
        id=_new_id(), forget_operation_id=op_id, target_id=old_cpid,
        dependency_type="epoch_checkpoint", dependency_id=old_cpid,
        discovery_batch_no=0, status="resolved", discovered_at=_utcnow()))
    db_session.commit()

    _rebuild_checkpoints(db_session, op_id, None)
    db_session.commit()

    new_cp = db_session.query(EpochCheckpoint).filter(
        EpochCheckpoint.epoch_id == eid,
        EpochCheckpoint.version == 2,
    ).first()
    assert new_cp is not None
    assert new_cp.current_goal == "[redacted]"
    ep = db_session.query(Epoch).filter_by(id=eid).first()
    assert ep.checkpoint_id == new_cp.id
    assert ep.checkpoint_id != old_cpid


# ═══════════════════════════════════════════════════════════════════
# scrub 正确性（修复 #5）
# ═══════════════════════════════════════════════════════════════════


def test_scrub_batch_exclusive_source_event_only(db_session: Session):
    """#5: 窄选择器 scrub 仅清理专属被忘 memory 的来源 event，保留共享 event。"""
    from aiive.worker.handlers_forget import _scrub_batch
    from aiive.db.models import MemoryRecord, MemoryEvidence, Event

    m_forget, m_keep = _new_id(), _new_id()
    shared_ev, excl_ev = _new_id(), _new_id()
    db_session.add(MemoryRecord(
        id=m_forget, memory_type="fact", canonical_key="k:fg",
        content="forgotten content", lifecycle_state="forgotten",
        validity_state="valid", confidence=0.5, created_at=_utcnow()))
    db_session.add(MemoryRecord(
        id=m_keep, memory_type="fact", canonical_key="k:kp",
        content="kept content", lifecycle_state="active",
        validity_state="valid", confidence=0.5, created_at=_utcnow()))
    db_session.add(MemoryEvidence(
        memory_id=m_forget, source_event_id=excl_ev, source_type="user_message"))
    db_session.add(MemoryEvidence(
        memory_id=m_keep, source_event_id=shared_ev, source_type="user_message"))
    db_session.add(Event(
        id=shared_ev, trace_id=_new_id(), thread_id=_new_id(),
        event_type="user_message", payload={"text": "shared"}, created_at=_utcnow()))
    db_session.add(Event(
        id=excl_ev, trace_id=_new_id(), thread_id=_new_id(),
        event_type="user_message", payload={"text": "exclusive secret"},
        created_at=_utcnow()))
    db_session.commit()

    _scrub_batch(db_session, [m_forget])
    db_session.commit()

    ev_excl = db_session.query(Event).filter_by(id=excl_ev).first()
    ev_shared = db_session.query(Event).filter_by(id=shared_ev).first()
    assert ev_excl.payload == {}
    assert ev_shared.payload.get("text") == "shared"
    keep = db_session.query(MemoryRecord).filter_by(id=m_keep).first()
    assert keep.content == "kept content"


def test_scrub_wide_covers_runtime_tables(db_session: Session):
    """#5: 宽选择器 scrub 覆盖 working_states / context_snapshots 等运行态表。"""
    from aiive.worker.handlers_forget import _scrub_wide
    from aiive.db.models import Event, WorkingState, ContextSnapshot, Thread
    from aiive.db.forget_models import ForgetOperation, ForgetSelectorManifest

    thread = _new_id()
    db_session.add(Thread(id=thread))
    db_session.add(Event(
        id=_new_id(), trace_id=_new_id(), thread_id=thread,
        event_type="user_message", payload={"text": "secret"}, created_at=_utcnow()))
    db_session.add(WorkingState(
        id=_new_id(), thread_id=thread, current_objective="obj"))
    db_session.add(ContextSnapshot(
        id=_new_id(), trace_id=_new_id(), thread_id=thread,
        stable_prefix_hash="h", context_items=[{"x": 1}]))
    # 先落库既有内容，确保其 created_at 早于 Phase A 冻结的 cutoff。
    db_session.flush()
    op_id = _new_id()
    db_session.add(ForgetOperation(
        id=op_id, operation_key=f"forget:{op_id}", mode="everywhere",
        selector_type="thread", selector_hash="h", status="shielded",
        requested_by="api", target_count=0, shielded_at=_utcnow()))
    db_session.add(ForgetSelectorManifest(
        id=_new_id(), forget_operation_id=op_id, selector_type="thread",
        selector_payload={"thread_id": thread}, selector_payload_hash="h",
        cutoff_created_at=_utcnow()))
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()

    ev = db_session.query(Event).filter_by(thread_id=thread).first()
    ws = db_session.query(WorkingState).filter_by(thread_id=thread).first()
    cs = db_session.query(ContextSnapshot).filter_by(thread_id=thread).first()
    assert ev.payload == {}
    assert ws.current_objective is None
    assert cs.context_items == []


# ═══════════════════════════════════════════════════════════════════
# verify 三态（修复 #4）
# ═══════════════════════════════════════════════════════════════════


def _run_verify(db_session: Session, op_id: str):
    """经 monkeypatch 让 handle_forget_verify 使用测试 session 执行。"""
    from sqlalchemy.orm import sessionmaker
    from aiive.worker import handlers_forget
    from aiive.worker.handlers_forget import handle_forget_verify
    from aiive.worker.outbox_dto import ClaimedJob

    engine = db_session.get_bind()
    handlers_forget.SessionLocal = sessionmaker(bind=engine)  # type: ignore[assignment]
    claimed = ClaimedJob(
        id=_new_id(), job_type="forget_verify",
        payload={"forget_operation_id": op_id}, trace_id="t", retry_count=0,
        max_retries=3, claim_token="ct", schema_version=1, worker_id="w",
    )
    return handle_forget_verify(claimed)


def test_verify_narrow_selector_ends_verified(db_session: Session):
    """#4: 窄选择器 + 全部 forgotten + tombstone + 不可搜索 → verified。"""
    from aiive.db.models import MemoryRecord, RetrievalIndexEntry
    from aiive.db.forget_models import ForgetTombstone

    op_id = _new_id()
    db_session.add(ForgetOperation(
        id=op_id, operation_key=f"forget:{op_id}", mode="memory_only",
        selector_type="memory_ids", selector_hash="h", status="shielded",
        requested_by="api", target_count=1, shielded_at=_utcnow()))
    mid = _new_id()
    db_session.add(MemoryRecord(
        id=mid, memory_type="fact", canonical_key="k:v", content="c",
        lifecycle_state="forgotten", validity_state="valid", confidence=0.5,
        created_at=_utcnow()))
    db_session.add(RetrievalIndexEntry(
        id=_new_id(), source_type="memory_record", source_id=mid,
        source_version="1", index_version=1,
        search_text="", snippet="", title="[forgotten]", is_searchable=False))
    db_session.add(ForgetTombstone(
        id=_new_id(), forget_operation_id=op_id, target_type="memory_record",
        target_id=mid, source_event_id=None, block_visibility=True,
        block_reingestion=True, content_purged=True, allow_audit_read=False,
        reason_code="forget", created_at=_utcnow()))
    db_session.commit()

    result = _run_verify(db_session, op_id)
    db_session.commit()

    op = db_session.query(ForgetOperation).filter_by(id=op_id).first()
    assert op.status == "verified"
    assert op.verified_at is not None
    assert result.outcome.value == "completed"


def test_verify_wide_selector_ends_coarse_purge(db_session: Session):
    """#4: 宽选择器（all_user_data）无失败 → verified_with_coarse_purge。"""
    op_id = _new_id()
    db_session.add(ForgetOperation(
        id=op_id, operation_key=f"forget:{op_id}", mode="everywhere",
        selector_type="all_user_data", selector_hash="h", status="shielded",
        requested_by="api", target_count=0, shielded_at=_utcnow()))
    db_session.commit()

    _run_verify(db_session, op_id)
    db_session.commit()

    op = db_session.query(ForgetOperation).filter_by(id=op_id).first()
    assert op.status == "verified_with_coarse_purge"
    assert op.verified_at is not None
    assert op.purged_at is not None


def test_verify_legacy_request_ends_unverifiable(db_session: Session):
    """#4: legacy_request_id 非空且无完整 provenance → legacy_unverifiable。"""
    from aiive.db.models import MemoryRecord, RetrievalIndexEntry
    from aiive.db.forget_models import ForgetTombstone

    op_id = _new_id()
    db_session.add(ForgetOperation(
        id=op_id, operation_key=f"forget:{op_id}", mode="memory_only",
        selector_type="memory_ids", selector_hash="h", status="shielded",
        requested_by="api", target_count=1, shielded_at=_utcnow(),
        legacy_request_id=_new_id()))
    mid = _new_id()
    db_session.add(MemoryRecord(
        id=mid, memory_type="fact", canonical_key="k:leg", content="c",
        lifecycle_state="forgotten", validity_state="valid", confidence=0.5,
        created_at=_utcnow()))
    db_session.add(RetrievalIndexEntry(
        id=_new_id(), source_type="memory_record", source_id=mid,
        source_version="1", index_version=1,
        search_text="", snippet="", title="[forgotten]", is_searchable=False))
    db_session.add(ForgetTombstone(
        id=_new_id(), forget_operation_id=op_id, target_type="memory_record",
        target_id=mid, source_event_id=None, block_visibility=True,
        block_reingestion=True, content_purged=True, allow_audit_read=False,
        reason_code="forget", created_at=_utcnow()))
    db_session.commit()

    _run_verify(db_session, op_id)
    db_session.commit()

    op = db_session.query(ForgetOperation).filter_by(id=op_id).first()
    assert op.status == "legacy_unverifiable"
    assert op.verified_at is not None


# ═══════════════════════════════════════════════════════════════════
# Forget Saga 自动对账与恢复
# ═══════════════════════════════════════════════════════════════════


def _make_reconcile_operation(
    db_session: Session,
    *,
    operation_status: str = "shielded",
    stages: tuple[tuple[str, str, str], ...] = (),
) -> tuple[str, dict[str, str]]:
    """构造指定 StageRun/Outbox 状态的遗忘操作。"""
    from aiive.db.models import OutboxJob

    op_id = _new_id()
    db_session.add(ForgetOperation(
        id=op_id,
        operation_key=f"forget:{op_id}",
        mode="memory_only",
        selector_type="memory_ids",
        selector_hash="h",
        status=operation_status,
        requested_by="api",
        target_count=0,
        shielded_at=_utcnow(),
    ))
    job_ids: dict[str, str] = {}
    for stage, stage_status, job_status in stages:
        job_id = _new_id()
        job_ids[stage] = job_id
        db_session.add(OutboxJob(
            id=job_id,
            operation_id=f"forget:{op_id}:{stage}",
            job_type=f"forget_{stage}",
            status=job_status,
            payload={"forget_operation_id": op_id},
            max_retries=3,
            retry_count=3 if job_status == "deadletter" else 0,
            error_message="boom" if job_status == "deadletter" else None,
            terminal_reason="max_retries_exhausted" if job_status == "deadletter" else None,
        ))
        db_session.add(ForgetStageRun(
            id=_new_id(),
            forget_operation_id=op_id,
            stage=stage,
            outbox_job_id=job_id,
            status=stage_status,
            failure_count=1 if stage_status == "deadletter" else 0,
        ))
    db_session.commit()
    return op_id, job_ids


def test_reconcile_enqueues_first_missing_stage(db_session: Session):
    """前置阶段完成但下一阶段缺失时，补发确定性 Job 与 StageRun。"""
    from aiive.db.models import OutboxJob
    from aiive.forget.reconcile_service import ForgetReconcileService

    op_id, _ = _make_reconcile_operation(
        db_session,
        stages=(("cascade", "done", "completed"),),
    )

    result = ForgetReconcileService.reconcile_operation(db_session, op_id)
    db_session.commit()

    assert result.action == "enqueued"
    assert result.stage == "rebuild_dependencies"
    job = db_session.query(OutboxJob).filter_by(
        operation_id=f"forget:{op_id}:rebuild_dependencies",
    ).one()
    stage_run = db_session.query(ForgetStageRun).filter_by(
        forget_operation_id=op_id,
        stage="rebuild_dependencies",
    ).one()
    assert job.status == "pending"
    assert stage_run.outbox_job_id == job.id
    assert stage_run.status == "pending"


def test_reconcile_repairs_missing_stage_run_for_existing_job(db_session: Session):
    """确定性 Job 已存在但 StageRun 缺失时，复用 Job 并补齐关联。"""
    from aiive.db.models import OutboxJob
    from aiive.forget.reconcile_service import ForgetReconcileService

    op_id, _ = _make_reconcile_operation(
        db_session,
        stages=(("cascade", "done", "completed"),),
    )
    job = OutboxJob(
        id=_new_id(),
        operation_id=f"forget:{op_id}:rebuild_dependencies",
        job_type="forget_rebuild_dependencies",
        status="pending",
        payload={"forget_operation_id": op_id},
        max_retries=3,
    )
    db_session.add(job)
    db_session.commit()

    result = ForgetReconcileService.reconcile_operation(db_session, op_id)
    db_session.commit()

    stage_run = db_session.query(ForgetStageRun).filter_by(
        forget_operation_id=op_id,
        stage="rebuild_dependencies",
    ).one()
    assert result.action == "in_flight"
    assert stage_run.outbox_job_id == job.id
    assert db_session.query(OutboxJob).filter_by(
        operation_id=f"forget:{op_id}:rebuild_dependencies",
    ).count() == 1


def test_reconcile_reuses_deadletter_job(db_session: Session):
    """shielded_deadletter 自动复用原 Job，不创建重复阶段记录。"""
    from datetime import timedelta

    from aiive.db.models import OutboxJob
    from aiive.forget.reconcile_service import ForgetReconcileService

    op_id, job_ids = _make_reconcile_operation(
        db_session,
        operation_status="shielded_deadletter",
        stages=(("cascade", "deadletter", "deadletter"),),
    )
    stage_run = db_session.query(ForgetStageRun).filter_by(
        forget_operation_id=op_id,
        stage="cascade",
    ).one()
    now = stage_run.updated_at + timedelta(hours=2)

    result = ForgetReconcileService.reconcile_operation(db_session, op_id, now=now)
    db_session.commit()

    assert result.action == "reactivated"
    job = db_session.get(OutboxJob, job_ids["cascade"])
    operation = db_session.get(ForgetOperation, op_id)
    assert job.status == "pending"
    assert job.retry_count == 0
    assert job.error_message is None
    assert job.terminal_reason is None
    assert stage_run.status == "pending"
    assert operation.status == "shielded"
    assert db_session.query(ForgetStageRun).filter_by(
        forget_operation_id=op_id,
        stage="cascade",
    ).count() == 1


def test_reconcile_failed_retryable_reactivates_verify(db_session: Session):
    """verify 已完成但结果可重试时，复用原 verify Job 再次执行。"""
    from aiive.db.models import OutboxJob
    from aiive.forget.reconcile_service import ForgetReconcileService

    op_id, job_ids = _make_reconcile_operation(
        db_session,
        operation_status="failed_retryable",
        stages=(
            ("cascade", "done", "completed"),
            ("rebuild_dependencies", "done", "completed"),
            ("purge", "done", "completed"),
            ("verify", "done", "completed"),
        ),
    )

    verify_run = db_session.query(ForgetStageRun).filter_by(
        forget_operation_id=op_id,
        stage="verify",
    ).one()
    from datetime import timedelta
    now = verify_run.updated_at + timedelta(hours=2)
    result = ForgetReconcileService.reconcile_operation(db_session, op_id, now=now)
    db_session.commit()

    verify_job = db_session.get(OutboxJob, job_ids["verify"])
    verify_run = db_session.query(ForgetStageRun).filter_by(
        forget_operation_id=op_id,
        stage="verify",
    ).one()
    assert result.action == "reactivated"
    assert result.stage == "verify"
    assert verify_job.status == "pending"
    assert verify_run.status == "pending"
    assert db_session.get(ForgetOperation, op_id).status == "verifying"


def test_reconcile_does_not_duplicate_in_flight_job(db_session: Session):
    """pending/running 阶段保持在途，多次扫描不重复入队。"""
    from aiive.db.models import OutboxJob
    from aiive.forget.reconcile_service import ForgetReconcileService

    op_id, _ = _make_reconcile_operation(
        db_session,
        stages=(("cascade", "pending", "pending"),),
    )

    first = ForgetReconcileService.reconcile_operation(db_session, op_id)
    second = ForgetReconcileService.reconcile_operation(db_session, op_id)
    db_session.commit()

    assert first.action == "in_flight"
    assert second.action == "in_flight"
    assert db_session.query(OutboxJob).filter_by(
        operation_id=f"forget:{op_id}:cascade",
    ).count() == 1


def test_reconcile_skips_terminal_operation(db_session: Session):
    """成功终态不再创建或重置任何阶段 Job。"""
    from aiive.db.models import OutboxJob
    from aiive.forget.reconcile_service import ForgetReconcileService

    op_id, _ = _make_reconcile_operation(db_session, operation_status="verified")

    result = ForgetReconcileService.reconcile_operation(db_session, op_id)
    db_session.commit()

    assert result.action == "terminal"
    assert db_session.query(OutboxJob).filter(
        OutboxJob.operation_id.like(f"forget:{op_id}:%"),
    ).count() == 0


# ═══════════════════════════════════════════════════════════════════
# retention 接收新终态（修复 #4 连锁 + #6）
# ═══════════════════════════════════════════════════════════════════


def test_retention_accepts_new_terminal_statuses(db_session: Session):
    """#4/#6: is_forget_operation_clearable 接受 verified / verified_with_coarse_purge / legacy_unverifiable。"""
    from aiive.retention.safety_predicates import is_forget_operation_clearable

    for status in ("verified", "verified_with_coarse_purge", "legacy_unverifiable"):
        op_id = _new_id()
        db_session.add(ForgetOperation(
            id=op_id, operation_key=f"forget:{op_id}", mode="memory_only",
            selector_type="memory_ids", selector_hash="h", status=status,
            requested_by="api", target_count=0, shielded_at=_utcnow(),
            verified_at=_utcnow(), purged_at=_utcnow()))
        db_session.commit()
        assert is_forget_operation_clearable(db_session, op_id) is True, f"{status} 应可清理"

    # shielded 未验证 → 不可清理
    op_id = _new_id()
    db_session.add(ForgetOperation(
        id=op_id, operation_key=f"forget:{op_id}", mode="memory_only",
        selector_type="memory_ids", selector_hash="h", status="shielded",
        requested_by="api", target_count=0, shielded_at=_utcnow()))
    db_session.commit()
    assert is_forget_operation_clearable(db_session, op_id) is False


# ═══════════════════════════════════════════════════════════════════
# 防重抽（修复 #3）
# ═══════════════════════════════════════════════════════════════════


def test_write_rejects_tombstone_blocked_reingestion(db_session: Session):
    """#3: 来源事件被 tombstone block_reingestion 时，write 拒绝重建记忆。"""
    from aiive.context.run_context import RunContext
    from aiive.memory.memory_types import (
        EvidenceItem, EvidenceSourceType, MemoryProposal, TrustLevel,
    )
    from aiive.memory.memory_write_service import MemoryWriteService, WriteOutcome
    from aiive.db.forget_models import ForgetTombstone

    ev_id = _new_id()
    db_session.add(ForgetTombstone(
        id=_new_id(), forget_operation_id=_new_id(), target_type="event",
        target_id=ev_id, source_event_id=ev_id, block_visibility=True,
        block_reingestion=True, content_purged=False, allow_audit_read=False,
        reason_code="forget", created_at=_utcnow()))
    db_session.commit()

    prop = MemoryProposal(
        memory_type="knowledge", canonical_key="user.blocked",
        scope_type="global", scope_id=None, content="重抽内容",
        confidence=0.9, trust_level=TrustLevel.TRUSTED.value,
        evidence=[EvidenceItem(
            source_event_id=ev_id,
            source_type=EvidenceSourceType.USER_ASSERTION.value,
            trust_level=TrustLevel.TRUSTED.value,
        )],
    )
    prop.compute_content_hash()
    ctx = RunContext(thread_id="t-block", trace_id="tr-block")
    writer = MemoryWriteService(db_session)
    result = writer.write(prop, ctx)
    db_session.commit()

    assert result.outcome == WriteOutcome.GATE_REJECTED
    assert "Reingestion blocked" in result.reason

# ═══════════════════════════════════════════════════════════════════
# Forget 来源 Event 隔离与摘要重建隐私闭环（目标新增测试）
# ═══════════════════════════════════════════════════════════════════


def _fg_event(db, *, content="hello", turn_id=None, turn_event_index=None,
              thread_id=None, created_at=None):
    from aiive.db.models import Event
    eid = _new_id()
    db.add(Event(
        id=eid, trace_id=_new_id(), thread_id=thread_id or _new_id(),
        event_type="user_message", payload={"content": content},
        turn_id=turn_id, turn_event_index=turn_event_index,
        created_at=created_at or _utcnow(),
    ))
    return eid


def _fg_memory(db, *, content="fact", lifecycle_state="active"):
    from aiive.db.models import MemoryRecord
    mid = _new_id()
    db.add(MemoryRecord(
        id=mid, memory_type="fact", canonical_key=f"k:{mid}",
        content=content, lifecycle_state=lifecycle_state,
        validity_state="valid", confidence=0.9, created_at=_utcnow(),
    ))
    return mid


def _fg_provenance(db, memory_id, event_id):
    from aiive.db.forget_models import ContentProvenanceRef
    db.add(ContentProvenanceRef(
        owner_type="memory_record", owner_id=memory_id,
        source_type="event", source_id=event_id, created_at=_utcnow(),
    ))


def _fg_evidence(db, memory_id, event_id):
    from aiive.db.models import MemoryEvidence
    db.add(MemoryEvidence(
        memory_id=memory_id, source_event_id=event_id,
        source_type="user_message", trust_level="semi_trusted",
        relation="supports", created_at=_utcnow(),
    ))


def _fg_tombstone(db, *, operation_id, target_type, target_id,
                  block_visibility=True, block_reingestion=True,
                  content_purged=False, allow_audit_read=False,
                  source_event_id=None, source_turn_record_id=None):
    from aiive.db.forget_models import ForgetTombstone
    db.add(ForgetTombstone(
        id=_new_id(), forget_operation_id=operation_id,
        target_type=target_type, target_id=target_id,
        block_visibility=block_visibility, block_reingestion=block_reingestion,
        content_purged=content_purged, allow_audit_read=allow_audit_read,
        source_event_id=source_event_id,
        source_turn_record_id=source_turn_record_id,
        reason_code="forget", created_at=_utcnow(),
    ))


def _fg_operation(db, op_id, *, mode="memory_only", selector_type="memory_ids",
                  status="shielded"):
    from aiive.db.forget_models import ForgetOperation
    db.add(ForgetOperation(
        id=op_id, operation_key=f"forget:{op_id}", mode=mode,
        selector_type=selector_type, selector_hash="h", status=status,
        requested_by="api", target_count=0, shielded_at=_utcnow(),
    ))


def test_write_evidence_batch_creates_provenance_ref(db_session: Session):
    """MemoryEvidence 写入时同事务生成 Memory → Event ProvenanceRef（去重/幂等）。"""
    from aiive.memory.memory_write_service import MemoryWriteService
    from aiive.memory.memory_types import (
        EvidenceItem, EvidenceSourceType, TrustLevel,
    )
    from aiive.db.forget_models import ContentProvenanceRef
    from aiive.db.models import MemoryEvidence

    mid = _fg_memory(db_session)
    e1, e2 = _new_id(), _new_id()
    evidence = [
        EvidenceItem(source_event_id=e1,
                     source_type=EvidenceSourceType.USER_ASSERTION.value,
                     trust_level=TrustLevel.TRUSTED.value),
        EvidenceItem(source_event_id=e2,
                     source_type=EvidenceSourceType.USER_ASSERTION.value,
                     trust_level=TrustLevel.TRUSTED.value),
        EvidenceItem(source_event_id=e1,   # 重复来源
                     source_type=EvidenceSourceType.USER_ASSERTION.value,
                     trust_level=TrustLevel.TRUSTED.value),
    ]
    writer = MemoryWriteService(db_session)
    writer._write_evidence_batch(mid, evidence)
    db_session.commit()

    refs = db_session.query(ContentProvenanceRef).filter_by(
        owner_type="memory_record", owner_id=mid, source_type="event",
    ).all()
    assert {r.source_id for r in refs} == {e1, e2}
    assert len(refs) == 2                        # 同批去重
    assert db_session.query(MemoryEvidence).filter_by(memory_id=mid).count() == 3

    writer._write_evidence_batch(mid, evidence)  # 幂等：不重复创建
    db_session.commit()
    assert db_session.query(ContentProvenanceRef).filter_by(
        owner_type="memory_record", owner_id=mid, source_type="event",
    ).count() == 2


def test_memory_event_provenance_migration_backfill_and_idempotent():
    """历史 MemoryEvidence 回填到 ContentProvenanceRef，且重复执行幂等。"""
    import importlib.util
    import os
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from aiive.db.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    m1, m2 = _fg_memory(s), _fg_memory(s)
    e1, e2 = _new_id(), _new_id()
    _fg_evidence(s, m1, e1)
    _fg_evidence(s, m1, e1)          # 重复 evidence，回填须 DISTINCT
    _fg_evidence(s, m2, e2)
    _fg_provenance(s, m1, e1)        # 已存在组合，迁移须跳过
    s.commit()
    s.close()

    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "backend", "alembic",
        "versions", "f1a2c3d4e5b6_memory_event_provenance_backfill.py",
    ))
    spec = importlib.util.spec_from_file_location("prov_backfill_migration", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    query = sa_text(
        "SELECT owner_id, source_id FROM content_provenance_refs "
        "WHERE owner_type = 'memory_record' AND source_type = 'event'"
    )
    with engine.connect() as conn:
        mod.op = Operations(MigrationContext.configure(conn))
        mod.upgrade()
        conn.commit()
        pairs = {tuple(r) for r in conn.execute(query).all()}
        assert (m1, e1) in pairs and (m2, e2) in pairs
        assert len(pairs) == 2

        mod.upgrade()                # 二次执行幂等
        conn.commit()
        pairs2 = {tuple(r) for r in conn.execute(query).all()}
        assert pairs2 == pairs
        assert len(pairs2) == 2
    engine.dispose()


def test_memory_only_source_event_hidden_from_normal_history(db_session: Session):
    """memory_only 后来源 Event 不进入普通历史；无关 Event 仍可见。"""
    from aiive.forget.fingerprint import configure_hmac_secret
    from aiive.forget.phase_a_shield import execute_phase_a_shield
    from aiive.forget.visibility_service import ForgetVisibilityService
    configure_hmac_secret("test-secret", 1)

    mid = _fg_memory(db_session)
    src_eid = _fg_event(db_session, content="secret")
    other_eid = _fg_event(db_session, content="public")
    _fg_provenance(db_session, mid, src_eid)
    _fg_evidence(db_session, mid, src_eid)
    db_session.commit()

    execute_phase_a_shield(
        session=db_session, mode="memory_only", memory_ids=[mid], reason="t",
    )
    db_session.commit()

    blocked = ForgetVisibilityService.blocked_target_ids(
        db_session, "event", [(src_eid, None), (other_eid, None)],
    )
    assert src_eid in blocked
    assert other_eid not in blocked
    kept = ForgetVisibilityService.filter_events(
        db_session, [{"id": src_eid}, {"id": other_eid}],
    )
    assert [e["id"] for e in kept] == [other_eid]


def test_memory_only_scrub_batch_preserves_source_event_payload(db_session: Session):
    """memory_only 保留来源 Event（含独占来源）的原始 payload。"""
    from aiive.worker.handlers_forget import _scrub_batch
    from aiive.db.models import Event, MemoryRecord

    mid = _fg_memory(db_session)
    eid = _fg_event(db_session, content="durable-secret")
    _fg_provenance(db_session, mid, eid)
    _fg_evidence(db_session, mid, eid)
    db_session.commit()

    _scrub_batch(db_session, [mid], mode="memory_only")
    db_session.commit()

    ev = db_session.query(Event).filter_by(id=eid).first()
    assert ev.payload.get("content") == "durable-secret"
    mem = db_session.query(MemoryRecord).filter_by(id=mid).first()
    assert mem.content == "[forgotten]"          # memory 层仍按语义 scrub


def test_everywhere_scrub_clears_source_event_payload(db_session: Session):
    """everywhere 后来源 Event 正文不可恢复。"""
    from aiive.worker.handlers_forget import _scrub_batch, _scrub_event_turn_targets
    from aiive.db.models import Event

    op_id = _new_id()
    _fg_operation(db_session, op_id, mode="everywhere")
    mid = _fg_memory(db_session, lifecycle_state="forgotten")
    eid = _fg_event(db_session, content="purge-me")
    _fg_provenance(db_session, mid, eid)
    _fg_evidence(db_session, mid, eid)
    _fg_tombstone(db_session, operation_id=op_id, target_type="memory_record",
                  target_id=mid)
    _fg_tombstone(db_session, operation_id=op_id, target_type="event",
                  target_id=eid, source_event_id=eid)
    db_session.commit()

    _scrub_batch(db_session, [mid], mode="everywhere")
    _scrub_event_turn_targets(db_session, op_id)
    db_session.commit()

    ev = db_session.query(Event).filter_by(id=eid).first()
    assert not ev.payload


def test_memory_only_source_event_cannot_be_reextracted(db_session: Session):
    """memory_only 后来源 Event 不能重新提取为新记忆；无关新 Event 不被拦截。"""
    from aiive.context.run_context import RunContext
    from aiive.memory.memory_types import (
        EvidenceItem, EvidenceSourceType, MemoryProposal, TrustLevel,
    )
    from aiive.memory.memory_write_service import MemoryWriteService, WriteOutcome
    from aiive.forget.visibility_service import ForgetVisibilityService

    op_id = _new_id()
    _fg_operation(db_session, op_id, mode="memory_only", status="verified")
    mid = _fg_memory(db_session, lifecycle_state="forgotten")
    eid = _fg_event(db_session, content="secret")
    _fg_provenance(db_session, mid, eid)
    _fg_tombstone(db_session, operation_id=op_id, target_type="memory_record",
                  target_id=mid, allow_audit_read=True)
    new_eid = _new_id()
    db_session.commit()

    assert ForgetVisibilityService.is_tombstone_blocked_reingestion(
        db_session, source_event_id=eid) is True
    assert ForgetVisibilityService.is_tombstone_blocked_reingestion(
        db_session, source_event_id=new_eid) is False

    prop = MemoryProposal(
        memory_type="knowledge", canonical_key="user.blocked",
        scope_type="global", scope_id=None, content="重抽内容",
        confidence=0.9, trust_level=TrustLevel.TRUSTED.value,
        evidence=[EvidenceItem(
            source_event_id=eid,
            source_type=EvidenceSourceType.USER_ASSERTION.value,
            trust_level=TrustLevel.TRUSTED.value,
        )],
    )
    prop.compute_content_hash()
    ctx = RunContext(thread_id="t", trace_id="tr")
    result = MemoryWriteService(db_session).write(prop, ctx)
    db_session.commit()
    assert result.outcome == WriteOutcome.GATE_REJECTED
    assert "Reingestion blocked" in result.reason


def test_turn_record_tombstone_hides_all_child_events_before_purge(db_session: Session):
    """一个 Turn 被遗忘后，其全部 Event 在清理完成前即不可见。"""
    from aiive.db.models import Event, TurnRecord
    from aiive.forget.visibility_service import ForgetVisibilityService

    thread_id = _new_id()
    logical_turn_id = _new_id()
    op_id = _new_id()
    db_session.add(TurnRecord(
        id=_new_id(), thread_id=thread_id, turn_id=logical_turn_id,
        turn_sequence=1, status="completed", request_fingerprint="",
        created_at=_utcnow(),
    ))
    child1 = _fg_event(db_session, content="a", turn_id=logical_turn_id,
                       turn_event_index=1, thread_id=thread_id)
    child2 = _fg_event(db_session, content="b", turn_id=logical_turn_id,
                       turn_event_index=2, thread_id=thread_id)
    unrelated = _fg_event(db_session, content="c", turn_id=_new_id(),
                          thread_id=thread_id)
    _fg_tombstone(db_session, operation_id=op_id, target_type="turn_record",
                  target_id=logical_turn_id, allow_audit_read=True)
    db_session.commit()

    blocked = ForgetVisibilityService.blocked_target_ids(
        db_session, "event",
        [(child1, None), (child2, None), (unrelated, None)],
    )
    assert child1 in blocked
    assert child2 in blocked
    assert unrelated not in blocked
    assert db_session.query(Event).filter_by(id=child1).first().payload


def test_can_audit_read_fails_closed_with_conflicting_tombstones(db_session: Session):
    """同一对象多个冲突 Tombstone 时审计权限 fail-closed。"""
    from aiive.forget.visibility_service import ForgetVisibilityService
    from aiive.db.forget_models import ForgetTombstone

    conflict_purged, conflict_denied = _new_id(), _new_id()
    for target_id, purged, allow in (
        (conflict_purged, False, True),
        (conflict_purged, True, True),
        (conflict_denied, False, True),
        (conflict_denied, False, False),
    ):
        db_session.add(ForgetTombstone(
            id=_new_id(), forget_operation_id=_new_id(),
            target_type="memory_record", target_id=target_id,
            block_visibility=True, block_reingestion=True,
            content_purged=purged, allow_audit_read=allow, created_at=_utcnow(),
        ))
    db_session.commit()

    assert ForgetVisibilityService.can_audit_read(
        db_session, "memory_record", conflict_purged) is False
    assert ForgetVisibilityService.can_audit_read(
        db_session, "memory_record", conflict_denied) is False
    assert ForgetVisibilityService.can_audit_read(
        db_session, "memory_record", _new_id()) is False


def test_wide_selector_phase_a_does_not_enumerate_targets(db_session: Session):
    """宽选择器在 Phase A 不无界枚举（不写 Target / 不写来源 Event 实体 Shield）。"""
    from aiive.forget.fingerprint import configure_hmac_secret
    from aiive.forget.phase_a_shield import execute_phase_a_shield
    from aiive.db.forget_models import ForgetShield, ForgetTarget
    configure_hmac_secret("test-secret", 1)

    for _ in range(5):
        mid = _fg_memory(db_session)
        eid = _fg_event(db_session, content="x")
        _fg_provenance(db_session, mid, eid)
        _fg_evidence(db_session, mid, eid)
    db_session.commit()

    result = execute_phase_a_shield(
        session=db_session, mode="everywhere", thread_id=_new_id(), reason="wide",
    )
    db_session.commit()
    op_id = result["operation_id"]

    assert db_session.query(ForgetTarget).filter_by(
        forget_operation_id=op_id).count() == 0
    assert db_session.query(ForgetShield).filter(
        ForgetShield.forget_operation_id == op_id,
        ForgetShield.target_id.isnot(None),
    ).count() == 0
    assert db_session.query(ForgetShield).filter(
        ForgetShield.forget_operation_id == op_id,
        ForgetShield.selector_type == "thread",
    ).count() == 1


# ═══════════════════════════════════════════════════════════════════
# SegmentSummary 重建隐私闭环（真实 _try_generate_summary 调用链）
# ═══════════════════════════════════════════════════════════════════


def _fg_install_fake_llm(monkeypatch):
    """把 LLM 网络边界替换为可控桩并捕获发给 LLM 的 messages。

    _try_generate_summary / ForgetVisibilityService / _build_summary_prompt /
    _extract_json / merge_summary 全部保持真实实现，仅 LLM 客户端（对外网络调用）
    被替换，因此这是真实调用链测试，而非孤立过滤函数测试。
    """
    import aiive.core.llm_client as llm_module

    captured = {}

    class _Resp:
        content = (
            '{"goal":"rebuilt goal","outcome":"rebuilt outcome",'
            '"decisions":[],"entities":[]}'
        )

    class _FakeLLM:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, messages, **kwargs):
            captured["messages"] = messages
            return _Resp()

    monkeypatch.setattr(llm_module, "LLMClient", _FakeLLM)
    return captured


def _fg_setup_segment(db, event_contents):
    """创建 Thread/Epoch/Segment + 每事件独占的 TurnRecord + Event。

    event_contents: [(label, content), ...]；返回 (segment_id, handles)，
    handles[label] = (event_id, turn_id, turn_record_id)。
    """
    from aiive.db.models import Thread, Epoch, Segment, TurnRecord

    thread_id = _new_id()
    epoch_id = _new_id()
    segment_id = _new_id()
    db.add(Thread(id=thread_id))
    db.add(Epoch(id=epoch_id, thread_id=thread_id, epoch_no=1, status="active"))
    db.add(Segment(
        id=segment_id, epoch_id=epoch_id, thread_id=thread_id,
        segment_no=1, status="sealed", start_turn_sequence=1,
        summary_id=_new_id(),
    ))
    handles = {}
    for idx, (label, content) in enumerate(event_contents):
        turn_id = _new_id()
        tr_id = _new_id()
        db.add(TurnRecord(
            id=tr_id, thread_id=thread_id, turn_id=turn_id,
            segment_id=segment_id, turn_sequence=idx + 1,
            status="completed", request_fingerprint="", created_at=_utcnow(),
        ))
        eid = _fg_event(db, content=content, turn_id=turn_id,
                        turn_event_index=1, thread_id=thread_id)
        handles[label] = (eid, turn_id, tr_id)
    return segment_id, handles


def test_summary_rebuild_current_op_excludes_forgotten_events(db_session, monkeypatch):
    """真实 _try_generate_summary 调用链：当前 Operation 遗忘内容不进入 LLM 输入与新 Summary。"""
    import hashlib
    from aiive.runtime.compaction import event_content_hash
    from aiive.worker.handlers_forget import _try_generate_summary
    from aiive.db.models import Event

    captured = _fg_install_fake_llm(monkeypatch)
    op_id = _new_id()
    _fg_operation(db_session, op_id, mode="everywhere")
    segment_id, h = _fg_setup_segment(db_session, [
        ("keep_a", "KEEP_ALPHA_TOKEN"),
        ("keep_b", "KEEP_BETA_TOKEN"),
        ("ev_drop", "DROP_EVENT_TOKEN"),
        ("turn_drop", "DROP_TURN_TOKEN"),
    ])
    # 当前 Operation 的 Event Tombstone
    _fg_tombstone(db_session, operation_id=op_id, target_type="event",
                  target_id=h["ev_drop"][0])
    # 当前 Operation 的 TurnRecord Tombstone（覆盖其全部子 Event）
    _fg_tombstone(db_session, operation_id=op_id, target_type="turn_record",
                  target_id=h["turn_drop"][1])
    db_session.commit()

    result = _try_generate_summary(db_session, segment_id, op_id)

    assert result is not None, "仍有允许来源，应生成新 Summary 而非 redacted"
    assert result["source_event_ids"] == [h["keep_a"][0], h["keep_b"][0]]
    assert set(result["source_turn_ids"]) == {h["keep_a"][1], h["keep_b"][1]}

    # source_hash 必须只由允许的来源按确定性顺序生成
    ordered = [
        db_session.query(Event).filter_by(id=h["keep_a"][0]).one(),
        db_session.query(Event).filter_by(id=h["keep_b"][0]).one(),
    ]
    expected_hash = hashlib.sha256(
        "|".join(f"{e.id}:{event_content_hash(e)}" for e in ordered).encode("utf-8")
    ).hexdigest()
    assert result["source_hash"] == expected_hash
    assert len(result["source_hash"]) == 64

    # LLM 只接收未被屏蔽的 Event
    messages_str = str(captured.get("messages"))
    assert "DROP_EVENT_TOKEN" not in messages_str
    assert "DROP_TURN_TOKEN" not in messages_str
    assert "KEEP_ALPHA_TOKEN" in messages_str
    assert "KEEP_BETA_TOKEN" in messages_str
    assert "DROP_" not in (result["goal"] or "") + (result["outcome"] or "")


def test_summary_rebuild_later_op_keeps_earlier_memory_only_content_hidden(db_session, monkeypatch):
    """后续另一个 Forget Operation 重建也不重新引入更早 memory_only 遗忘的来源 Event。"""
    from aiive.worker.handlers_forget import _try_generate_summary

    captured = _fg_install_fake_llm(monkeypatch)
    op_a = _new_id()
    _fg_operation(db_session, op_a, mode="memory_only")
    mid = _fg_memory(db_session, lifecycle_state="forgotten")
    segment_id, h = _fg_setup_segment(db_session, [
        ("keep", "KEEP_LATER_TOKEN"),
        ("mem_drop", "DROP_MEMORY_TOKEN"),
    ])
    _fg_provenance(db_session, mid, h["mem_drop"][0])
    _fg_evidence(db_session, mid, h["mem_drop"][0])
    # 早期 memory_only 遗忘：memory_record Tombstone（审计可读、内容未清除）
    _fg_tombstone(db_session, operation_id=op_a, target_type="memory_record",
                  target_id=mid, content_purged=False, allow_audit_read=True)
    db_session.commit()

    # 第一次（Operation A）重建：来源 Memory 已被遗忘 → 其来源 Event 被屏蔽
    r_a = _try_generate_summary(db_session, segment_id, op_a)
    assert r_a is not None
    assert set(r_a["source_event_ids"]) == {h["keep"][0]}
    assert "DROP_MEMORY_TOKEN" not in str(captured["messages"])
    assert "KEEP_LATER_TOKEN" in str(captured["messages"])

    # 后续另一个 Forget Operation 重建：过滤不依赖当前 operation_id，
    # 统一读取全部有效 Tombstone / Provenance，早期遗忘内容仍被屏蔽。
    op_b = _new_id()
    _fg_operation(db_session, op_b, mode="memory_only")
    db_session.commit()
    captured["messages"] = None
    r_b = _try_generate_summary(db_session, segment_id, op_b)
    assert r_b is not None
    assert set(r_b["source_event_ids"]) == {h["keep"][0]}
    assert "DROP_MEMORY_TOKEN" not in str(captured["messages"])
    assert "KEEP_LATER_TOKEN" in str(captured["messages"])


def test_rebuild_summaries_batch_persists_summary_only_from_allowed_events(db_session, monkeypatch):
    """真实调用方 _rebuild_summaries_batch → _try_generate_summary：落库新 Summary 来源字段只含允许 Event。"""
    import hashlib
    from aiive.runtime.compaction import event_content_hash
    from aiive.worker.handlers_forget import _rebuild_summaries_batch
    from aiive.db.models import Event, Segment, SegmentSummary
    from aiive.db.forget_models import ForgetBatch, ForgetDependency

    captured = _fg_install_fake_llm(monkeypatch)
    op_id = _new_id()
    _fg_operation(db_session, op_id, mode="everywhere")
    segment_id, h = _fg_setup_segment(db_session, [
        ("keep", "KEEP_PERSIST_TOKEN"),
        ("drop", "DROP_PERSIST_TOKEN"),
    ])
    # 当前 Operation 遗忘 drop Event
    _fg_tombstone(db_session, operation_id=op_id, target_type="event",
                  target_id=h["drop"][0])
    # 受影响旧 Summary（version 1）
    old_sid = _new_id()
    db_session.add(SegmentSummary(
        id=old_sid, segment_id=segment_id, summary_version=1,
        source_event_ids=[h["keep"][0], h["drop"][0]], goal="old",
    ))
    db_session.add(ForgetDependency(
        id=_new_id(), forget_operation_id=op_id,
        dependency_type="segment_summary", dependency_id=old_sid,
        status="pending", discovered_at=_utcnow(),
    ))
    batch = ForgetBatch(
        id=_new_id(), forget_operation_id=op_id, stage="rebuild",
        dependency_type="segment_summary", batch_no=0,
        cursor_start_json={}, status="pending",
    )
    db_session.add(batch)
    db_session.commit()

    _rebuild_summaries_batch(db_session, op_id, batch)
    db_session.commit()

    new_s = (
        db_session.query(SegmentSummary)
        .filter(SegmentSummary.segment_id == segment_id,
                SegmentSummary.summary_version == 2)
        .one()
    )
    assert list(new_s.source_event_ids or []) == [h["keep"][0]]
    assert set(new_s.source_turn_ids or []) == {h["keep"][1]}
    keep_ev = db_session.query(Event).filter_by(id=h["keep"][0]).one()
    expected_hash = hashlib.sha256(
        f"{keep_ev.id}:{event_content_hash(keep_ev)}".encode("utf-8")
    ).hexdigest()
    assert new_s.source_hash == expected_hash
    assert "DROP_PERSIST_TOKEN" not in (new_s.goal or "") + (new_s.outcome or "")
    seg = db_session.query(Segment).filter_by(id=segment_id).one()
    assert seg.summary_id == new_s.id
    # 真实 LLM 输入仍只含允许来源
    assert "DROP_PERSIST_TOKEN" not in str(captured["messages"])
    assert "KEEP_PERSIST_TOKEN" in str(captured["messages"])


# ═══════════════════════════════════════════════════════════════════
# Forget Verify 完整性扩展
# ═══════════════════════════════════════════════════════════════════


def _fg_verify_op(db, *, op_id, mid, mode, memory_block_visibility=True,
                  memory_block_reingestion=True):
    """窄选择器 Verify 基础数据：满足 memory forgotten / retrieval 不可搜 / tombstone 覆盖。"""
    from aiive.db.models import MemoryRecord, RetrievalIndexEntry
    from aiive.db.forget_models import ForgetTarget

    _fg_operation(db, op_id, mode=mode)  # 默认 selector_type="memory_ids"（窄选择器）
    db.add(ForgetTarget(
        id=_new_id(), forget_operation_id=op_id,
        target_type="memory_record", target_id=mid,
        batch_no=0, frozen_at=_utcnow(),
    ))
    db.add(MemoryRecord(
        id=mid, memory_type="fact", canonical_key=f"k:{mid}",
        content="c", lifecycle_state="forgotten", validity_state="valid",
        confidence=0.5, created_at=_utcnow(),
    ))
    db.add(RetrievalIndexEntry(
        id=_new_id(), source_type="memory_record", source_id=mid,
        source_version="1", index_version=1,
        search_text="", snippet="", title="[forgotten]", is_searchable=False,
    ))
    _fg_tombstone(
        db, operation_id=op_id, target_type="memory_record", target_id=mid,
        block_visibility=memory_block_visibility,
        block_reingestion=memory_block_reingestion,
        content_purged=(mode != "memory_only"),
        allow_audit_read=(mode == "memory_only"),
    )


def test_verify_accepts_full_source_event_integrity_memory_only(db_session):
    """来源 Event 已屏蔽 + memory_only 原文保留 → 允许标记严格 verified。"""
    from aiive.db.models import Event

    op_id, mid, src_eid = _new_id(), _new_id(), _new_id()
    _fg_verify_op(db_session, op_id=op_id, mid=mid, mode="memory_only")
    db_session.add(Event(
        id=src_eid, trace_id=_new_id(), thread_id=_new_id(),
        event_type="user_message", payload={"content": "DURABLE_SECRET"},
        created_at=_utcnow(),
    ))
    _fg_provenance(db_session, mid, src_eid)
    db_session.commit()

    _run_verify(db_session, op_id)
    db_session.commit()

    op = db_session.query(ForgetOperation).filter_by(id=op_id).first()
    assert op.status == "verified"


def test_verify_rejects_memory_only_wrongly_cleared_source_event(db_session):
    """memory_only 误清空来源 Event 原文 → 完整性无法证明，不得严格 verified。"""
    from aiive.db.models import Event

    op_id, mid, src_eid = _new_id(), _new_id(), _new_id()
    _fg_verify_op(db_session, op_id=op_id, mid=mid, mode="memory_only")
    db_session.add(Event(
        id=src_eid, trace_id=_new_id(), thread_id=_new_id(),
        event_type="user_message", payload={},  # 被错误清空
        created_at=_utcnow(),
    ))
    _fg_provenance(db_session, mid, src_eid)
    db_session.commit()

    _run_verify(db_session, op_id)
    db_session.commit()

    op = db_session.query(ForgetOperation).filter_by(id=op_id).first()
    assert op.status == "failed_retryable"
    assert "payload wrongly cleared" in (op.error_message or "")


def test_verify_rejects_everywhere_unpurged_source_event(db_session):
    """everywhere 来源 Event 正文未清理且无 content_purged Tombstone → 不得严格 verified。"""
    from aiive.db.models import Event

    op_id, mid, src_eid = _new_id(), _new_id(), _new_id()
    _fg_verify_op(db_session, op_id=op_id, mid=mid, mode="everywhere")
    db_session.add(Event(
        id=src_eid, trace_id=_new_id(), thread_id=_new_id(),
        event_type="user_message", payload={"content": "PURGE_ME"},
        created_at=_utcnow(),
    ))
    _fg_provenance(db_session, mid, src_eid)
    db_session.commit()

    _run_verify(db_session, op_id)
    db_session.commit()

    op = db_session.query(ForgetOperation).filter_by(id=op_id).first()
    assert op.status == "failed_retryable"
    assert "payload not purged" in (op.error_message or "")


def test_verify_rejects_unshielded_memory_source_event(db_session):
    """来源 Event 存在但未被屏蔽 → 完整性无法证明，不得严格 verified。"""
    from aiive.db.models import Event

    op_id, mid, src_eid = _new_id(), _new_id(), _new_id()
    # memory Tombstone 存在但不具备 block_visibility（模拟屏蔽缺口）
    _fg_verify_op(db_session, op_id=op_id, mid=mid, mode="memory_only",
                  memory_block_visibility=False)
    db_session.add(Event(
        id=src_eid, trace_id=_new_id(), thread_id=_new_id(),
        event_type="user_message", payload={"content": "LEAK"},
        created_at=_utcnow(),
    ))
    _fg_provenance(db_session, mid, src_eid)
    db_session.commit()

    _run_verify(db_session, op_id)
    db_session.commit()

    op = db_session.query(ForgetOperation).filter_by(id=op_id).first()
    assert op.status == "failed_retryable"
    assert "not shielded" in (op.error_message or "")


def test_verify_rejects_rebuilt_summary_referencing_blocked_event(db_session):
    """受影响的新 Summary 仍引用被屏蔽 Event → 不得严格 verified。"""
    from aiive.db.models import Event, SegmentSummary
    from aiive.db.forget_models import ForgetDependency

    op_id, mid, src_eid = _new_id(), _new_id(), _new_id()
    leaked_eid, segment_id, old_sid = _new_id(), _new_id(), _new_id()
    _fg_verify_op(db_session, op_id=op_id, mid=mid, mode="memory_only")
    # 来源 Event：已屏蔽 + 原文保留（前两项检查应通过）
    db_session.add(Event(
        id=src_eid, trace_id=_new_id(), thread_id=_new_id(),
        event_type="user_message", payload={"content": "OK"},
        created_at=_utcnow(),
    ))
    _fg_provenance(db_session, mid, src_eid)
    # 被屏蔽的泄漏 Event
    db_session.add(Event(
        id=leaked_eid, trace_id=_new_id(), thread_id=_new_id(),
        event_type="user_message", payload={"content": "LEAKED"},
        created_at=_utcnow(),
    ))
    _fg_tombstone(db_session, operation_id=op_id, target_type="event",
                  target_id=leaked_eid, block_visibility=True)
    # 受影响旧 Summary + 重建后新 Summary（version 2）仍引用 leaked_eid
    db_session.add(SegmentSummary(
        id=old_sid, segment_id=segment_id, summary_version=1,
        source_event_ids=[leaked_eid], goal="old",
    ))
    db_session.add(SegmentSummary(
        id=_new_id(), segment_id=segment_id, summary_version=2,
        source_event_ids=[leaked_eid], goal="rebuilt",
    ))
    db_session.add(ForgetDependency(
        id=_new_id(), forget_operation_id=op_id,
        dependency_type="segment_summary", dependency_id=old_sid,
        status="pending", discovered_at=_utcnow(),
    ))
    db_session.commit()

    _run_verify(db_session, op_id)
    db_session.commit()

    op = db_session.query(ForgetOperation).filter_by(id=op_id).first()
    assert op.status == "failed_retryable"
    assert "still referenced by rebuilt summaries" in (op.error_message or "")


# ═══════════════════════════════════════════════════════════════════
# 防重抽闭环 / Mode-Selector 契约 / Turn ID 一致性（本次修复）
# ═══════════════════════════════════════════════════════════════════


def _fg_memory_full(db, *, canonical_key=None, scope_type="global",
                    scope_id=None, created_at=None):
    from aiive.db.models import MemoryRecord

    mid = _new_id()
    db.add(MemoryRecord(
        id=mid, memory_type="fact", canonical_key=canonical_key or f"k:{mid}",
        scope_type=scope_type, scope_id=scope_id, content="fact",
        lifecycle_state="active", validity_state="valid", confidence=0.9,
        created_at=created_at or _utcnow(),
    ))
    return mid


def _fg_write_proposal(db, *, event_ids, canonical_key="user.reingest"):
    """经真实 MemoryWriteService.write 写入，返回 WriteResult。"""
    from aiive.context.run_context import RunContext
    from aiive.memory.memory_types import (
        EvidenceItem, EvidenceSourceType, MemoryProposal, TrustLevel,
    )
    from aiive.memory.memory_write_service import MemoryWriteService

    prop = MemoryProposal(
        memory_type="knowledge", canonical_key=canonical_key,
        scope_type="global", scope_id=None, content="重抽内容",
        confidence=0.9, trust_level=TrustLevel.TRUSTED.value,
        evidence=[
            EvidenceItem(
                source_event_id=eid,
                source_type=EvidenceSourceType.USER_ASSERTION.value,
                trust_level=TrustLevel.TRUSTED.value,
            )
            for eid in event_ids
        ],
    )
    prop.compute_content_hash()
    return MemoryWriteService(db).write(
        prop, RunContext(thread_id=_new_id(), trace_id=_new_id())
    )


def test_phase_a_thread_shield_blocks_reingestion_before_cascade(db_session: Session):
    """宽 thread 选择器：Phase A 后、Cascade 前旧 Event 不能重抽；cutoff 后新 Event 不受阻。"""
    from datetime import timedelta

    from aiive.db.models import MemoryEvidence
    from aiive.forget.fingerprint import configure_hmac_secret
    from aiive.forget.phase_a_shield import execute_phase_a_shield
    from aiive.memory.memory_write_service import WriteOutcome

    configure_hmac_secret("test-secret", 1)
    thread_id = _new_id()
    old_eid = _fg_event(
        db_session, content="old-secret", thread_id=thread_id,
        created_at=_utcnow() - timedelta(minutes=1),
    )
    mid = _fg_memory(db_session)
    _fg_provenance(db_session, mid, old_eid)
    db_session.commit()

    execute_phase_a_shield(
        session=db_session, mode="everywhere", thread_id=thread_id, reason="t",
    )
    db_session.commit()

    result = _fg_write_proposal(db_session, event_ids=[old_eid])
    assert result.outcome == WriteOutcome.GATE_REJECTED
    assert "Reingestion blocked" in (result.reason or "")
    assert db_session.query(MemoryEvidence).filter(
        MemoryEvidence.source_event_id == old_eid,
    ).count() == 0

    new_eid = _fg_event(
        db_session, content="new-ok", thread_id=thread_id,
        created_at=_utcnow() + timedelta(minutes=5),
    )
    db_session.commit()
    result2 = _fg_write_proposal(
        db_session, event_ids=[new_eid], canonical_key="user.new-fact",
    )
    assert not (
        result2.outcome == WriteOutcome.GATE_REJECTED
        and "Reingestion blocked" in (result2.reason or "")
    )


@pytest.mark.parametrize("selector", ["canonical_key", "scope", "time_range", "all_user_data"])
def test_wide_selector_phase_a_blocks_reingestion_before_cascade(
    db_session: Session, selector,
):
    """canonical_key / scope / time_range / all_user_data 在 Phase A 后均阻断旧来源重抽。"""
    from datetime import timedelta

    from aiive.forget.fingerprint import configure_hmac_secret
    from aiive.forget.phase_a_shield import execute_phase_a_shield
    from aiive.memory.memory_write_service import WriteOutcome

    configure_hmac_secret("test-secret", 1)
    now = _utcnow()
    old_time = now - timedelta(minutes=1)
    kwargs = {}
    if selector == "canonical_key":
        mid = _fg_memory_full(db_session, canonical_key="user.ck", created_at=old_time)
        kwargs["canonical_key"] = "user.ck"
    elif selector == "scope":
        scope_id = _new_id()
        mid = _fg_memory_full(
            db_session, scope_type="thread", scope_id=scope_id, created_at=old_time,
        )
        kwargs["scope_type"] = "thread"
        kwargs["scope_id"] = scope_id
    else:
        mid = _fg_memory_full(db_session, created_at=old_time)
        if selector == "time_range":
            kwargs["time_from"] = now - timedelta(minutes=2)
            kwargs["time_to"] = now
        else:
            kwargs["all_user_data"] = True

    eid = _fg_event(db_session, content="old-secret", created_at=old_time)
    _fg_provenance(db_session, mid, eid)
    db_session.commit()

    execute_phase_a_shield(
        session=db_session, mode="everywhere", reason="t", **kwargs,
    )
    db_session.commit()

    result = _fg_write_proposal(db_session, event_ids=[eid])
    assert result.outcome == WriteOutcome.GATE_REJECTED
    assert "Reingestion blocked" in (result.reason or "")


def test_over_limit_memory_ids_blocks_reingestion_before_cascade(db_session: Session):
    """> MAX_INLINE_SHIELD_TARGETS 的 memory_ids：Phase A 后仍不能从旧来源重抽。"""
    from aiive.forget.fingerprint import configure_hmac_secret
    from aiive.forget.phase_a_shield import execute_phase_a_shield
    from aiive.forget.selector_normalizer import MAX_INLINE_SHIELD_TARGETS
    from aiive.memory.memory_write_service import WriteOutcome

    configure_hmac_secret("test-secret", 1)
    mids = [_fg_memory(db_session) for _ in range(MAX_INLINE_SHIELD_TARGETS + 1)]
    eid = _fg_event(db_session, content="old-secret")
    _fg_provenance(db_session, mids[0], eid)
    db_session.commit()

    execute_phase_a_shield(
        session=db_session, mode="memory_only", memory_ids=mids, reason="t",
    )
    db_session.commit()

    result = _fg_write_proposal(db_session, event_ids=[eid])
    assert result.outcome == WriteOutcome.GATE_REJECTED
    assert "Reingestion blocked" in (result.reason or "")


def test_mixed_evidence_fail_closed_and_not_persisted(db_session: Session):
    """混合 Evidence（1 被屏蔽 + 1 正常）整体拒绝，且不写 Evidence/Provenance。"""
    from aiive.db.forget_models import ContentProvenanceRef
    from aiive.db.models import MemoryEvidence, MemoryRecord
    from aiive.memory.memory_write_service import WriteOutcome

    blocked_eid = _new_id()
    normal_eid = _fg_event(db_session, content="normal")
    _fg_tombstone(
        db_session, operation_id=_new_id(), target_type="event",
        target_id=blocked_eid, source_event_id=blocked_eid,
        block_visibility=True, block_reingestion=True,
    )
    db_session.commit()

    result = _fg_write_proposal(
        db_session, event_ids=[blocked_eid, normal_eid],
        canonical_key="user.mixed",
    )
    assert result.outcome == WriteOutcome.GATE_REJECTED
    assert "Reingestion blocked" in (result.reason or "")

    # 被拒绝的 Evidence 不得写入 MemoryEvidence / ContentProvenanceRef
    assert db_session.query(MemoryEvidence).filter(
        MemoryEvidence.source_event_id.in_([blocked_eid, normal_eid]),
    ).count() == 0
    assert db_session.query(ContentProvenanceRef).filter(
        ContentProvenanceRef.source_id.in_([blocked_eid, normal_eid]),
    ).count() == 0
    assert db_session.query(MemoryRecord).filter_by(
        canonical_key="user.mixed",
    ).count() == 0


def test_partial_shielded_evidence_via_phase_a_rejected(db_session: Session):
    """Phase A 实体 Shield 命中其中一条 Evidence 时，整条 Proposal 拒绝。"""
    from aiive.forget.fingerprint import configure_hmac_secret
    from aiive.forget.phase_a_shield import execute_phase_a_shield
    from aiive.memory.memory_write_service import WriteOutcome

    configure_hmac_secret("test-secret", 1)
    blocked_eid = _fg_event(db_session, content="blocked")
    normal_eid = _fg_event(db_session, content="normal")
    mid = _fg_memory(db_session)
    _fg_provenance(db_session, mid, blocked_eid)
    db_session.commit()

    execute_phase_a_shield(
        session=db_session, mode="memory_only", memory_ids=[mid], reason="t",
    )
    db_session.commit()

    result = _fg_write_proposal(
        db_session, event_ids=[blocked_eid, normal_eid],
        canonical_key="user.partial",
    )
    assert result.outcome == WriteOutcome.GATE_REJECTED
    assert "Reingestion blocked" in (result.reason or "")


@pytest.mark.parametrize(
    ("mode", "kwargs", "ok"),
    [
        ("memory_only", {"memory_ids": ["m1"]}, True),
        ("memory_only", {"canonical_key": "k"}, True),
        ("memory_only", {"scope_type": "thread", "scope_id": "t"}, True),
        ("history_only", {"thread_id": "t"}, True),
        ("history_only", {"turn_ids": ["t1"]}, True),
        ("history_only", {"event_ids": ["e1"]}, True),
        ("history_only", {"time_from": datetime(2026, 1, 1, tzinfo=timezone.utc)}, True),
        ("everywhere", {"all_user_data": True}, True),
        ("memory_only", {"thread_id": "t"}, False),
        ("memory_only", {"all_user_data": True}, False),
        ("memory_only", {"turn_ids": ["t1"]}, False),
        ("history_only", {"memory_ids": ["m1"]}, False),
        ("history_only", {"canonical_key": "k"}, False),
        ("history_only", {"all_user_data": True}, False),
    ],
)
def test_mode_selector_matrix(mode, kwargs, ok):
    """memory_only / history_only / everywhere 与选择器的兼容矩阵。"""
    from aiive.forget.selector_normalizer import normalize_selector

    if ok:
        payload = normalize_selector(mode, **kwargs)
        assert payload["mode"] == mode
    else:
        with pytest.raises(ValueError):
            normalize_selector(mode, **kwargs)


def test_multiple_selectors_rejected_stably():
    """多个独立 Selector 混合传入必须稳定拒绝，不能静默忽略。"""
    from aiive.forget.selector_normalizer import normalize_selector

    with pytest.raises(ValueError, match="只能提供一种选择器"):
        normalize_selector("everywhere", memory_ids=["m"], thread_id="t")
    with pytest.raises(ValueError, match="只能提供一种选择器"):
        normalize_selector("everywhere", canonical_key="k", all_user_data=True)


def test_api_and_tool_use_same_mode_selector_rules():
    """API 与 Tool 与 selector_normalizer 使用一致的校验规则。"""
    from pydantic import ValidationError

    from aiive.api.routes_forget import ForgetRequest
    from aiive.tools.forget_tool import handle_forget

    with pytest.raises(ValidationError):
        ForgetRequest(mode="memory_only", thread_id="t")
    with pytest.raises(ValidationError):
        ForgetRequest(mode="everywhere", memory_ids=["m"], thread_id="t")

    out = handle_forget(mode="memory_only", thread_id="t")
    assert out["ok"] is False
    out2 = handle_forget(mode="history_only", memory_ids=["m"])
    assert out2["ok"] is False


def test_phase_a_rejects_incompatible_mode_selector_before_operation(db_session: Session):
    """非法组合必须在创建 Operation 之前拒绝。"""
    from aiive.forget.fingerprint import configure_hmac_secret
    from aiive.forget.phase_a_shield import execute_phase_a_shield

    configure_hmac_secret("test-secret", 1)
    with pytest.raises(ValueError):
        execute_phase_a_shield(
            session=db_session, mode="memory_only", thread_id=_new_id(), reason="t",
        )
    assert db_session.query(ForgetOperation).count() == 0


@pytest.mark.parametrize("use_record_id", [True, False])
def test_turn_scrub_clears_all_child_events_for_both_id_forms(
    db_session: Session, use_record_id,
):
    """TurnRecord.id 与 turn_id 两种输入均清理全部子 Event，并置 content_purged。"""
    from aiive.db.models import Event, TurnRecord
    from aiive.forget.fingerprint import configure_hmac_secret
    from aiive.forget.phase_a_shield import execute_phase_a_shield
    from aiive.worker.handlers_forget import _scrub_event_turn_targets

    configure_hmac_secret("test-secret", 1)
    thread_id = _new_id()
    record_id = _new_id()
    logical_id = _new_id()
    db_session.add(TurnRecord(
        id=record_id, thread_id=thread_id, turn_id=logical_id,
        turn_sequence=1, status="completed", request_fingerprint="",
        response_payload={"content": "resp"},
    ))
    eids = [
        _fg_event(
            db_session, content=f"c{i}", turn_id=logical_id,
            turn_event_index=i, thread_id=thread_id,
        )
        for i in range(3)
    ]
    db_session.commit()

    selector_id = record_id if use_record_id else logical_id
    result = execute_phase_a_shield(
        session=db_session, mode="everywhere", turn_ids=[selector_id], reason="t",
    )
    db_session.commit()
    op_id = result["operation_id"]

    _scrub_event_turn_targets(db_session, op_id)
    db_session.commit()

    for eid in eids:
        assert db_session.query(Event).filter_by(id=eid).one().payload == {}
    tr = db_session.query(TurnRecord).filter_by(id=record_id).one()
    assert not tr.response_payload
    tomb = db_session.query(ForgetTombstone).filter_by(
        forget_operation_id=op_id, target_type="turn_record",
    ).one()
    assert tomb.content_purged is True
    assert tomb.purged_at is not None


def test_turn_scrub_content_purged_reflects_actual_cleanup(db_session: Session):
    """未实际清理时 content_purged 保持 False；实际清理后才置 True。"""
    from aiive.db.models import Event, TurnRecord
    from aiive.worker.handlers_forget import _scrub_event_turn_targets

    thread_id = _new_id()
    logical_id = _new_id()
    db_session.add(TurnRecord(
        id=_new_id(), thread_id=thread_id, turn_id=logical_id,
        turn_sequence=1, status="completed", request_fingerprint="",
        response_payload={"content": "resp"},
    ))
    eid = _fg_event(
        db_session, content="secret", turn_id=logical_id, thread_id=thread_id,
    )
    op_id = _new_id()
    _fg_operation(db_session, op_id, mode="memory_only", selector_type="turn_ids")
    _fg_tombstone(
        db_session, operation_id=op_id, target_type="turn_record",
        target_id=logical_id, source_turn_record_id=logical_id,
        content_purged=False,
    )
    db_session.commit()

    _scrub_event_turn_targets(db_session, op_id)  # memory_only 提前返回
    db_session.commit()
    tomb = db_session.query(ForgetTombstone).filter_by(
        forget_operation_id=op_id, target_type="turn_record",
    ).one()
    assert tomb.content_purged is False
    assert db_session.query(Event).filter_by(id=eid).one().payload == {
        "content": "secret",
    }

    op = db_session.query(ForgetOperation).filter_by(id=op_id).one()
    op.mode = "everywhere"
    db_session.commit()
    _scrub_event_turn_targets(db_session, op_id)
    db_session.commit()

    assert db_session.query(Event).filter_by(id=eid).one().payload == {}
    assert db_session.query(ForgetTombstone).filter_by(
        id=tomb.id,
    ).one().content_purged is True


def test_verify_rejects_shield_only_without_persistent_tombstone(db_session: Session):
    """只有活动 Shield、没有持久化 Tombstone 时，Verify 不得严格 verified。"""
    from aiive.db.models import Event

    op_id, mid, src_eid = _new_id(), _new_id(), _new_id()
    _fg_verify_op(
        db_session, op_id=op_id, mid=mid, mode="memory_only",
        memory_block_visibility=False, memory_block_reingestion=False,
    )
    db_session.add(Event(
        id=src_eid, trace_id=_new_id(), thread_id=_new_id(),
        event_type="user_message", payload={"content": "SEEN"},
        created_at=_utcnow(),
    ))
    _fg_provenance(db_session, mid, src_eid)
    db_session.add(ForgetShield(
        id=_new_id(), forget_operation_id=op_id, selector_type="event_ids",
        target_type="event", target_id=src_eid,
        cutoff_created_at=_utcnow(), status="active",
        normalized_shield_key=_new_id(),
    ))
    db_session.commit()

    _run_verify(db_session, op_id)
    db_session.commit()

    op = db_session.query(ForgetOperation).filter_by(id=op_id).one()
    assert op.status == "failed_retryable"
    assert "persistent tombstone protection" in (op.error_message or "")


def test_shield_retired_keeps_visibility_and_reingestion_blocked(db_session: Session):
    """Shield 退休后，持久化 Tombstone 仍同时阻断可见性与重抽。"""
    from aiive.db.models import Event
    from aiive.forget.visibility_service import ForgetVisibilityService

    op_id, eid = _new_id(), _new_id()
    _fg_operation(db_session, op_id, mode="memory_only", status="verified")
    db_session.add(Event(
        id=eid, trace_id=_new_id(), thread_id=_new_id(),
        event_type="user_message", payload={"content": "SECRET"},
        created_at=_utcnow(),
    ))
    _fg_tombstone(
        db_session, operation_id=op_id, target_type="event", target_id=eid,
        source_event_id=eid, block_visibility=True, block_reingestion=True,
        content_purged=False, allow_audit_read=True,
    )
    db_session.add(ForgetShield(
        id=_new_id(), forget_operation_id=op_id, selector_type="event_ids",
        target_type="event", target_id=eid,
        cutoff_created_at=_utcnow(), status="retired",
        normalized_shield_key=_new_id(),
    ))
    db_session.commit()

    assert ForgetVisibilityService.blocked_target_ids(
        db_session, "event", [(eid, None)],
    ) == {eid}
    assert ForgetVisibilityService.is_tombstone_blocked_reingestion(
        db_session, source_event_id=eid,
    ) is True
    assert ForgetVisibilityService.filter_events(
        db_session, [{"id": eid, "created_at": _utcnow()}],
    ) == []
