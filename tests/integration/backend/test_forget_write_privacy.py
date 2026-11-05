"""Forget × MemoryWrite 拒绝路径隐私闭环回归测试（SQLite 可验证部分）。

覆盖目标：
  1. Forget 防重抽检查必须先于 Gate Reject 的 Proposal 持久化：被屏蔽来源的
     原文不得经 memory_proposals 重新落库。
  2. 普通 Gate Reject（未命中 Forget）保留原有审计行。
  3. 真实 MemoryWriteService.write() / write_batch() 调用链的整体拒绝。
  4. 纯 Turn 来源（无 Event ID）拒绝，且 Forget 之后的新 Turn 不被阻断。
  5. 强制决策 / Reinforce / Merge / Supersede 不能绕过检查。

PostgreSQL 并发语义（共享/排他 advisory lock 的两种时序）见
tests/integration/pg/test_pg_forget_write_concurrency.py。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from aiive.context.run_context import RunContext
from aiive.db.forget_models import ContentProvenanceRef, ForgetTombstone
from aiive.db.models import (
    Event,
    MemoryEvidence,
    MemoryIngestionRun,
    MemoryRecord,
    MemoryProposal as MemoryProposalModel,
    Thread,
    TurnRecord,
)
from aiive.memory.memory_types import (
    EvidenceItem,
    EvidenceSourceType,
    MemoryProposal,
    TrustLevel,
    WriteOutcome,
)
from aiive.memory.memory_write_service import (
    REINGESTION_BLOCKED_REASON,
    MemoryWriteService,
)
from sqlalchemy.orm import Session


def _new_id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 种子数据
# ---------------------------------------------------------------------------


def _event(db: Session, *, content: str = "secret", turn_id: str | None = None,
           thread_id: str | None = None, created_at: datetime | None = None) -> str:
    eid = _new_id()
    db.add(Event(
        id=eid, trace_id=_new_id(), thread_id=thread_id or _new_id(),
        event_type="user_message", payload={"content": content},
        turn_id=turn_id, created_at=created_at or _utcnow(),
    ))
    return eid


def _thread(db: Session, thread_id: str) -> str:
    db.add(Thread(id=thread_id))
    return thread_id


def _turn(db: Session, thread_id: str, *, turn_id: str | None = None,
          created_at: datetime | None = None) -> TurnRecord:
    turn = TurnRecord(
        id=_new_id(), thread_id=thread_id, turn_id=turn_id or _new_id(),
        turn_sequence=1, status="completed", created_at=created_at or _utcnow(),
    )
    db.add(turn)
    db.flush()
    return turn


def _tombstone(db: Session, *, target_type: str, target_id: str,
               source_event_id: str | None = None,
               source_turn_record_id: str | None = None) -> None:
    db.add(ForgetTombstone(
        id=_new_id(), forget_operation_id=_new_id(),
        target_type=target_type, target_id=target_id,
        block_visibility=True, block_reingestion=True,
        content_purged=False, allow_audit_read=False,
        source_event_id=source_event_id,
        source_turn_record_id=source_turn_record_id,
        reason_code="forget", created_at=_utcnow(),
    ))


def _proposal(
    *,
    content: str,
    canonical_key: str = "user.secret",
    event_ids: list[str] | None = None,
    source_event_ids: list[str] | None = None,
    source_turn_record_id: str = "",
    source_turn_id: str = "",
    durable: bool = True,
    structured_value: dict[str, object] | None = None,
    raw_payload: dict[str, object] | None = None,
) -> MemoryProposal:
    evidence = [
        EvidenceItem(
            source_event_id=eid,
            source_type=EvidenceSourceType.USER_ASSERTION.value,
            trust_level=TrustLevel.TRUSTED.value,
        )
        for eid in (event_ids or [])
    ]
    prop = MemoryProposal(
        memory_type="knowledge",
        canonical_key=canonical_key,
        scope_type="global",
        scope_id=None,
        content=content,
        structured_value=structured_value,
        confidence=0.9,
        trust_level=TrustLevel.TRUSTED.value,
        durable=durable,
        evidence=evidence,
        source_event_ids=list(source_event_ids or []),
        source_turn_record_id=source_turn_record_id,
        source_turn_id=source_turn_id,
        raw_payload=raw_payload,
        normalized_payload={"text": content},
    )
    prop.compute_content_hash()
    return prop


def _ctx(thread_id: str | None = None, *, turn_id: str = "",
         turn_record_id: str = "") -> RunContext:
    return RunContext(
        thread_id=thread_id or _new_id(), trace_id=_new_id(),
        turn_id=turn_id, turn_record_id=turn_record_id,
    )


def _ingestion_run(db: Session, turn: TurnRecord) -> MemoryIngestionRun:
    run = MemoryIngestionRun(
        source_turn_record_id=turn.id, extractor_name="test",
        extractor_version="1.0", status="running",
    )
    db.add(run)
    db.flush()
    return run


def _assert_no_content_leak(db: Session, secret: str) -> None:
    """四类表（MemoryRecord / MemoryEvidence / ContentProvenanceRef / Proposal）无原文残留。"""
    leaks: list[str] = []
    for rec in db.query(MemoryRecord).all():
        if secret in repr((rec.content, rec.structured_value, rec.graph_facts)):
            leaks.append(f"memory_records:{rec.id}")
    for ev in db.query(MemoryEvidence).all():
        if secret in repr((ev.content_span,)):
            leaks.append(f"memory_evidence:{ev.id}")
    for mp in db.query(MemoryProposalModel).all():
        if secret in repr((
            mp.content, mp.structured_value, mp.evidence,
            mp.raw_payload, mp.normalized_payload,
        )):
            leaks.append(f"memory_proposals:{mp.id}")
    assert not leaks, f"被 Forget 拒绝的内容仍然落库: {leaks}"


def _assert_no_side_effects(db: Session, secret: str) -> None:
    assert db.query(MemoryRecord).count() == 0
    assert db.query(MemoryEvidence).count() == 0
    assert db.query(ContentProvenanceRef).count() == 0
    assert db.query(MemoryProposalModel).count() == 0
    _assert_no_content_leak(db, secret)


# ---------------------------------------------------------------------------
# A. Gate Reject 隐私闭环
# ---------------------------------------------------------------------------


def test_gate_reject_blocked_source_does_not_persist_proposal(db_session: Session):
    """被屏蔽 Event + Gate Reject（durable=False）：不得写入 memory_proposals。"""
    secret = "SECRET-GATE-REJECT-001"
    blocked = _event(db_session, content=secret)
    _tombstone(
        db_session, target_type="event", target_id=blocked,
        source_event_id=blocked,
    )
    db_session.commit()

    prop = _proposal(
        content=secret, event_ids=[blocked], durable=False,
        structured_value={"raw": secret}, raw_payload={"text": secret},
    )
    result = MemoryWriteService(db_session).write(prop, _ctx())
    db_session.commit()

    assert result.outcome == WriteOutcome.GATE_REJECTED
    assert result.reason == REINGESTION_BLOCKED_REASON
    _assert_no_side_effects(db_session, secret)


def test_gate_accept_blocked_source_does_not_persist_proposal(db_session: Session):
    """被屏蔽 Event + 正常 Gate（会通过）：同样整体拒绝且不落任何内容。"""
    secret = "SECRET-GATE-ACCEPT-002"
    blocked = _event(db_session, content=secret)
    _tombstone(
        db_session, target_type="event", target_id=blocked,
        source_event_id=blocked,
    )
    db_session.commit()

    prop = _proposal(
        content=secret, event_ids=[blocked], durable=True,
        structured_value={"raw": secret}, raw_payload={"text": secret},
    )
    result = MemoryWriteService(db_session).write(prop, _ctx())
    db_session.commit()

    assert result.outcome == WriteOutcome.GATE_REJECTED
    assert "Reingestion blocked" in (result.reason or "")
    _assert_no_side_effects(db_session, secret)


def test_plain_gate_reject_keeps_audit_row(db_session: Session):
    """未命中 Forget 的普通 Gate Reject：保留原有 Proposal 审计行。"""
    secret = "PLAIN-REJECT-AUDIT-003"
    prop = _proposal(content=secret, durable=False)
    result = MemoryWriteService(db_session).write(prop, _ctx())
    db_session.commit()

    assert result.outcome == WriteOutcome.GATE_REJECTED
    assert "Reingestion" not in (result.reason or "")

    rows = db_session.query(MemoryProposalModel).all()
    assert len(rows) == 1
    assert rows[0].content == secret
    assert rows[0].final_operation == "reject"
    # 审计行不等于记忆：记忆与证据仍为空
    assert db_session.query(MemoryRecord).count() == 0
    assert db_session.query(MemoryEvidence).count() == 0


def test_write_batch_gate_reject_blocked_source_does_not_persist_proposal(
    db_session: Session,
):
    """write_batch 路径的 Gate Reject 同样不得持久化被屏蔽来源的 Proposal。"""
    secret = "SECRET-BATCH-REJECT-004"
    thread_id = _thread(db_session, _new_id())
    turn = _turn(db_session, thread_id)
    run = _ingestion_run(db_session, turn)
    blocked = _event(db_session, content=secret, thread_id=thread_id)
    _tombstone(
        db_session, target_type="event", target_id=blocked,
        source_event_id=blocked,
    )
    db_session.commit()

    prop = _proposal(
        content=secret, event_ids=[blocked], durable=False,
        raw_payload={"text": secret},
    )
    results = MemoryWriteService(db_session).write_batch(
        proposals=[prop], ingestion_run=run, run_context=_ctx(thread_id),
    )
    db_session.commit()

    assert results[0].outcome == WriteOutcome.GATE_REJECTED
    _assert_no_side_effects(db_session, secret)


# ---------------------------------------------------------------------------
# B. 真实 write() / write_batch() 调用链
# ---------------------------------------------------------------------------


def test_write_batch_all_blocked_produces_no_memory_or_copy(db_session: Session):
    """批次内全部 Proposal 均来自被屏蔽来源：不产生任何记忆/证据/Proposal 副本。"""
    thread_id = _thread(db_session, _new_id())
    turn = _turn(db_session, thread_id)
    run = _ingestion_run(db_session, turn)
    blocked_a = _event(db_session, content="SECRET-BATCH-A", thread_id=thread_id)
    blocked_b = _event(db_session, content="SECRET-BATCH-B", thread_id=thread_id)
    for eid in (blocked_a, blocked_b):
        _tombstone(db_session, target_type="event", target_id=eid, source_event_id=eid)
    db_session.commit()

    props = [
        _proposal(content="SECRET-BATCH-A", canonical_key="user.a", event_ids=[blocked_a]),
        _proposal(content="SECRET-BATCH-B", canonical_key="user.b", event_ids=[blocked_b]),
    ]
    results = MemoryWriteService(db_session).write_batch(
        proposals=props, ingestion_run=run, run_context=_ctx(thread_id),
    )
    db_session.commit()

    assert [r.outcome for r in results] == [WriteOutcome.GATE_REJECTED] * 2
    _assert_no_side_effects(db_session, "SECRET-BATCH-A")
    _assert_no_content_leak(db_session, "SECRET-BATCH-B")


def test_write_batch_mixed_evidence_rejects_whole_proposal(db_session: Session):
    """混合 Evidence（1 条被屏蔽 + 1 条正常）整体拒绝，正常来源也不写入。"""
    thread_id = _thread(db_session, _new_id())
    turn = _turn(db_session, thread_id)
    run = _ingestion_run(db_session, turn)
    blocked = _event(db_session, content="SECRET-MIXED", thread_id=thread_id)
    normal = _event(db_session, content="normal", thread_id=thread_id)
    _tombstone(db_session, target_type="event", target_id=blocked, source_event_id=blocked)
    db_session.commit()

    prop = _proposal(
        content="SECRET-MIXED", canonical_key="user.mixed",
        event_ids=[blocked, normal],
    )
    results = MemoryWriteService(db_session).write_batch(
        proposals=[prop], ingestion_run=run, run_context=_ctx(thread_id),
    )
    db_session.commit()

    assert results[0].outcome == WriteOutcome.GATE_REJECTED
    assert db_session.query(MemoryEvidence).filter(
        MemoryEvidence.source_event_id.in_([blocked, normal]),
    ).count() == 0
    assert db_session.query(ContentProvenanceRef).filter(
        ContentProvenanceRef.source_id.in_([blocked, normal]),
    ).count() == 0
    assert db_session.query(MemoryRecord).count() == 0
    _assert_no_content_leak(db_session, "SECRET-MIXED")


def test_pure_turn_source_blocked_by_turn_entity_shield(db_session: Session):
    """纯 Turn 来源（无 Event ID）被 Phase A turn Shield 拦截。"""
    from aiive.forget.phase_a_shield import execute_phase_a_shield

    thread_id = _thread(db_session, _new_id())
    turn = _turn(db_session, thread_id)
    db_session.commit()

    execute_phase_a_shield(
        session=db_session, mode="history_only", turn_ids=[turn.turn_id],
        reason="pure-turn",
    )
    db_session.commit()

    secret = "SECRET-PURE-TURN-006"
    prop = _proposal(
        content=secret, source_turn_record_id=turn.id, source_turn_id=turn.turn_id,
    )
    result = MemoryWriteService(db_session).write(prop, _ctx(thread_id))
    db_session.commit()

    assert result.outcome == WriteOutcome.GATE_REJECTED
    assert "Reingestion blocked" in (result.reason or "")
    _assert_no_side_effects(db_session, secret)


def test_pure_turn_source_blocked_by_turn_record_id_shield(db_session: Session):
    """Turn Shield 以 TurnRecord.id 写入时同样拦截逻辑 turn_id 形式的来源。"""
    from aiive.forget.phase_a_shield import execute_phase_a_shield

    thread_id = _thread(db_session, _new_id())
    turn = _turn(db_session, thread_id)
    db_session.commit()

    execute_phase_a_shield(
        session=db_session, mode="history_only", turn_ids=[turn.id], reason="turn-id",
    )
    db_session.commit()

    secret = "SECRET-PURE-TURN-ID-007"
    prop = _proposal(content=secret, source_turn_record_id=turn.turn_id)
    result = MemoryWriteService(db_session).write(prop, _ctx(thread_id))
    db_session.commit()

    assert result.outcome == WriteOutcome.GATE_REJECTED
    _assert_no_side_effects(db_session, secret)


def test_pure_turn_source_blocked_by_tombstone(db_session: Session):
    """无 Shield、仅有持久化 block_reingestion Turn Tombstone 时也拦截。"""
    thread_id = _thread(db_session, _new_id())
    turn = _turn(db_session, thread_id)
    _tombstone(
        db_session, target_type="turn_record", target_id=turn.turn_id,
        source_turn_record_id=turn.turn_id,
    )
    db_session.commit()

    secret = "SECRET-PURE-TURN-TOMB-008"
    prop = _proposal(content=secret, source_turn_record_id=turn.id)
    result = MemoryWriteService(db_session).write(prop, _ctx(thread_id))
    db_session.commit()

    assert result.outcome == WriteOutcome.GATE_REJECTED
    _assert_no_side_effects(db_session, secret)


def test_pure_turn_new_turn_after_forget_is_not_blocked(db_session: Session):
    """Forget 之后产生的新 Turn 不被旧 thread 选择器 Shield 阻断。"""
    from aiive.forget.phase_a_shield import execute_phase_a_shield

    thread_id = _thread(db_session, _new_id())
    _turn(db_session, thread_id)
    db_session.commit()

    execute_phase_a_shield(
        session=db_session, mode="history_only", thread_id=thread_id, reason="thread",
    )
    db_session.commit()

    new_turn = _turn(
        db_session, thread_id,
        created_at=_utcnow() + timedelta(minutes=5),
    )
    db_session.commit()

    content = "用户主动重新提供的新事实"
    prop = _proposal(
        content=content, canonical_key="user.new-fact",
        source_turn_record_id=new_turn.id, source_turn_id=new_turn.turn_id,
    )
    result = MemoryWriteService(db_session).write(prop, _ctx(thread_id))
    db_session.commit()

    assert result.outcome == WriteOutcome.WRITTEN, result.reason
    assert db_session.query(MemoryRecord).filter_by(
        canonical_key="user.new-fact",
    ).count() == 1


def test_forced_decision_cannot_bypass_forget_check(db_session: Session):
    """用户强制决策（accept_new）不能绕过 Forget 防重抽。"""
    secret = "SECRET-FORCED-009"
    old = MemoryRecord(
        id=_new_id(), memory_type="knowledge", canonical_key="user.forced",
        content="旧记忆", lifecycle_state="active", validity_state="valid",
        confidence=0.9, created_at=_utcnow(),
    )
    db_session.add(old)
    blocked = _event(db_session, content=secret)
    _tombstone(db_session, target_type="event", target_id=blocked, source_event_id=blocked)
    db_session.commit()

    prop = _proposal(
        content=secret, canonical_key="user.forced", event_ids=[blocked],
    )
    result = MemoryWriteService(db_session).write(
        prop, _ctx(), decision="accept_new", decision_target_id=old.id,
    )
    db_session.commit()

    assert result.outcome == WriteOutcome.GATE_REJECTED
    _assert_no_content_leak(db_session, secret)
    assert db_session.query(MemoryRecord).count() == 1  # 仅原有旧记忆


def test_reinforce_path_cannot_bypass_forget_check(db_session: Session):
    """命中已有同键记录（会被 Reinforce）时仍先拒绝，不改写旧记录。"""
    secret = "SECRET-REINFORCE-010"
    old = MemoryRecord(
        id=_new_id(), memory_type="knowledge", canonical_key="user.reinforce",
        content="用户喜欢喝咖啡", lifecycle_state="active", validity_state="valid",
        confidence=0.5, reinforce_count=0, record_version=1, created_at=_utcnow(),
    )
    db_session.add(old)
    blocked = _event(db_session, content=secret)
    _tombstone(db_session, target_type="event", target_id=blocked, source_event_id=blocked)
    db_session.commit()

    prop = _proposal(
        content="用户喜欢喝咖啡", canonical_key="user.reinforce",
        event_ids=[blocked],
    )
    result = MemoryWriteService(db_session).write(prop, _ctx())
    db_session.commit()

    assert result.outcome == WriteOutcome.GATE_REJECTED
    refreshed = db_session.get(MemoryRecord, old.id)
    assert refreshed is not None
    assert refreshed.confidence == pytest.approx(0.5)
    assert refreshed.record_version == 1
    assert db_session.query(MemoryEvidence).count() == 0
    _assert_no_content_leak(db_session, secret)


def test_supersede_path_cannot_bypass_forget_check(db_session: Session):
    """会被 Supersede 的提案同样先拒绝：旧记录保持 active/valid 且无新记录。"""
    secret = "SECRET-SUPERSEDE-011"
    old = MemoryRecord(
        id=_new_id(), memory_type="knowledge", canonical_key="user.supersede",
        content="用户住在北京", lifecycle_state="active", validity_state="valid",
        confidence=0.9, record_version=1, created_at=_utcnow(),
    )
    db_session.add(old)
    blocked = _event(db_session, content=secret)
    _tombstone(db_session, target_type="event", target_id=blocked, source_event_id=blocked)
    db_session.commit()

    prop = _proposal(
        content="用户现在住在上海", canonical_key="user.supersede",
        event_ids=[blocked],
    )
    result = MemoryWriteService(db_session).write(prop, _ctx())
    db_session.commit()

    assert result.outcome == WriteOutcome.GATE_REJECTED
    assert db_session.query(MemoryRecord).count() == 1
    refreshed = db_session.get(MemoryRecord, old.id)
    assert refreshed is not None
    assert refreshed.validity_state == "valid"
    assert refreshed.lifecycle_state == "active"
    _assert_no_content_leak(db_session, secret)


def test_merge_path_cannot_bypass_forget_check(db_session: Session):
    """Merge 分支同样不能绕过：不产生新记录、不写证据。"""
    secret = "SECRET-MERGE-012"
    old = MemoryRecord(
        id=_new_id(), memory_type="knowledge", canonical_key="user.merge",
        content="用户喜欢咖啡", lifecycle_state="active", validity_state="valid",
        confidence=0.9, record_version=1, created_at=_utcnow(),
    )
    db_session.add(old)
    blocked = _event(db_session, content=secret)
    _tombstone(db_session, target_type="event", target_id=blocked, source_event_id=blocked)
    db_session.commit()

    prop = _proposal(
        content="用户喜欢喝咖啡和茶", canonical_key="user.merge",
        event_ids=[blocked],
    )
    result = MemoryWriteService(db_session).write(prop, _ctx())
    db_session.commit()

    assert result.outcome == WriteOutcome.GATE_REJECTED
    assert db_session.query(MemoryRecord).count() == 1
    assert db_session.query(MemoryEvidence).count() == 0
    _assert_no_content_leak(db_session, secret)


def test_run_context_turn_source_is_checked(db_session: Session):
    """Proposal 自身不带来源，仅 RunContext 携带被遗忘 Turn：也必须拒绝。"""
    from aiive.forget.phase_a_shield import execute_phase_a_shield

    thread_id = _thread(db_session, _new_id())
    turn = _turn(db_session, thread_id)
    db_session.commit()

    execute_phase_a_shield(
        session=db_session, mode="history_only", turn_ids=[turn.turn_id], reason="ctx",
    )
    db_session.commit()

    secret = "SECRET-RUNCTX-013"
    prop = _proposal(content=secret, canonical_key="user.runctx")
    result = MemoryWriteService(db_session).write(
        prop, _ctx(thread_id, turn_id=turn.turn_id, turn_record_id=turn.id),
    )
    db_session.commit()

    assert result.outcome == WriteOutcome.GATE_REJECTED
    _assert_no_side_effects(db_session, secret)
