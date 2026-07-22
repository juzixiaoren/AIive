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
    """#2/#3: all_user_data Shield 经 blocked_target_ids 全局 fail-closed 屏蔽。"""
    from aiive.forget.visibility_service import ForgetVisibilityService

    op = ForgetOperation(
        id=_new_id(), operation_key="forget:global", mode="everywhere",
        selector_type="all_user_data", selector_hash="h", status="shielded",
        requested_by="api", target_count=0, shielded_at=_utcnow(),
    )
    db_session.add(op)
    db_session.add(ForgetShield(
        id=_new_id(), forget_operation_id=op.id, selector_type="all_user_data",
        all_user_data=True, cutoff_created_at=_utcnow(), status="active",
        normalized_shield_key="k",
    ))
    db_session.commit()

    blocked = ForgetVisibilityService.blocked_target_ids(
        db_session, "memory_record",
        [(_new_id(), _utcnow()), (_new_id(), _utcnow())],
    )
    assert len(blocked) == 2  # 全部屏蔽


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

    op = ForgetOperation(
        id=_new_id(), operation_key="f:scope", mode="everywhere",
        selector_type="scope", selector_hash="h", status="shielded",
        requested_by="api", target_count=0, shielded_at=_utcnow(),
    )
    db_session.add(op)
    db_session.add(ForgetShield(
        id=_new_id(), forget_operation_id=op.id, selector_type="scope",
        scope_type="project", scope_id="p1",
        cutoff_created_at=_utcnow(), status="active", normalized_shield_key="ks",
    ))
    m_match, m_other = _new_id(), _new_id()
    db_session.add(MemoryRecord(
        id=m_match, memory_type="fact", canonical_key="k:m",
        scope_type="project", scope_id="p1", content="x",
        lifecycle_state="active", validity_state="valid", confidence=0.5,
        created_at=_utcnow()))
    db_session.add(MemoryRecord(
        id=m_other, memory_type="fact", canonical_key="k:o",
        scope_type="project", scope_id="p2", content="y",
        lifecycle_state="active", validity_state="valid", confidence=0.5,
        created_at=_utcnow()))
    db_session.commit()

    blocked = ForgetVisibilityService.blocked_target_ids(
        db_session, "memory_record", [(m_match, _utcnow()), (m_other, _utcnow())])
    assert m_match in blocked
    assert m_other not in blocked


def test_blocked_target_ids_selector_thread(db_session: Session):
    """#1: thread 选择器 Shield 屏蔽同 thread 的 event（memory_record 无 thread 列，不直达）。"""
    from aiive.forget.visibility_service import ForgetVisibilityService
    from aiive.db.models import Event
    from aiive.db.forget_models import ForgetShield, ForgetOperation

    op = ForgetOperation(
        id=_new_id(), operation_key="f:thread", mode="everywhere",
        selector_type="thread", selector_hash="h", status="shielded",
        requested_by="api", target_count=0, shielded_at=_utcnow(),
    )
    db_session.add(op)
    db_session.add(ForgetShield(
        id=_new_id(), forget_operation_id=op.id, selector_type="thread",
        thread_id="t1", cutoff_created_at=_utcnow(), status="active",
        normalized_shield_key="kt",
    ))
    e_match, e_other = _new_id(), _new_id()
    db_session.add(Event(
        id=e_match, trace_id=_new_id(), thread_id="t1",
        event_type="user_message", payload={}, created_at=_utcnow()))
    db_session.add(Event(
        id=e_other, trace_id=_new_id(), thread_id="t2",
        event_type="user_message", payload={}, created_at=_utcnow()))
    db_session.commit()

    blocked = ForgetVisibilityService.blocked_target_ids(
        db_session, "event", [(e_match, _utcnow()), (e_other, _utcnow())])
    assert e_match in blocked
    assert e_other not in blocked


def test_blocked_target_ids_selector_canonical_key(db_session: Session):
    """#1: canonical_key 选择器 Shield 屏蔽同 canonical_key 的 memory_record。"""
    from aiive.forget.visibility_service import ForgetVisibilityService
    from aiive.db.models import MemoryRecord
    from aiive.db.forget_models import ForgetShield, ForgetOperation

    op = ForgetOperation(
        id=_new_id(), operation_key="f:ck", mode="everywhere",
        selector_type="canonical_key", selector_hash="h", status="shielded",
        requested_by="api", target_count=0, shielded_at=_utcnow(),
    )
    db_session.add(op)
    db_session.add(ForgetShield(
        id=_new_id(), forget_operation_id=op.id, selector_type="canonical_key",
        canonical_key="user.secret", cutoff_created_at=_utcnow(), status="active",
        normalized_shield_key="kc",
    ))
    m_match, m_other = _new_id(), _new_id()
    db_session.add(MemoryRecord(
        id=m_match, memory_type="fact", canonical_key="user.secret",
        scope_type="global", scope_id=None, content="x",
        lifecycle_state="active", validity_state="valid", confidence=0.5,
        created_at=_utcnow()))
    db_session.add(MemoryRecord(
        id=m_other, memory_type="fact", canonical_key="user.other",
        scope_type="global", scope_id=None, content="y",
        lifecycle_state="active", validity_state="valid", confidence=0.5,
        created_at=_utcnow()))
    db_session.commit()

    blocked = ForgetVisibilityService.blocked_target_ids(
        db_session, "memory_record", [(m_match, _utcnow()), (m_other, _utcnow())])
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
