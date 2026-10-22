"""remember_or_update 幂等键彻底修复回归测试。

覆盖：
1. 来源为空时，不同内容必须产生不同幂等键（修复前退化为固定常量 key，
   导致每次调用共享同一键而触发唯一约束冲突）。
2. 完全相同的请求（同来源 + 同内容）保持稳定去重键。
3. _persist_proposal 在唯一约束冲突时跳过而非污染整条事务。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aiive.db.models import Base, MemoryProposal as MemoryProposalModel
from aiive.memory.memory_types import MemoryProposal, TrustLevel
from aiive.memory.memory_gate import GateDecision
from aiive.memory.memory_write_service import MemoryWriteService


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def _make_proposal(content: str, source_event_ids: list[str] | None = None) -> MemoryProposal:
    return MemoryProposal(
        memory_type="knowledge",
        canonical_key=f"knowledge.{content[:20]}",
        content=content,
        trust_level=TrustLevel.TRUSTED.value,
        extractor_name="remember_or_update_tool",
        extractor_version="1.0",
        source_event_ids=source_event_ids or [],
    )


def test_idempotency_key_distinct_for_distinct_content_with_empty_source():
    """来源为空时，不同内容必须产生不同键 —— 这是修复前的崩溃根因。"""
    p1 = _make_proposal("用户喜欢咖啡")
    p2 = _make_proposal("用户喜欢茶")
    p1.compute_request_idempotency()
    p2.compute_request_idempotency()
    assert p1.idempotency_key != p2.idempotency_key
    # 不再是全调用共享的固定常量
    assert not p1.idempotency_key.endswith("69bfabb765b725bd5ad5c7ef")


def test_idempotency_key_stable_for_identical_request():
    """同来源 + 同内容 → 稳定相同键，保证重试去重。"""
    p1 = _make_proposal("用户喜欢咖啡", source_event_ids=["evt-1"])
    p2 = _make_proposal("用户喜欢咖啡", source_event_ids=["evt-1"])
    p1.compute_request_idempotency()
    p2.compute_request_idempotency()
    assert p1.idempotency_key == p2.idempotency_key


def test_idempotency_key_distinguishes_source():
    """不同来源 → 不同键。"""
    p1 = _make_proposal("用户喜欢咖啡", source_event_ids=["evt-1"])
    p2 = _make_proposal("用户喜欢咖啡", source_event_ids=["evt-2"])
    p1.compute_request_idempotency()
    p2.compute_request_idempotency()
    assert p1.idempotency_key != p2.idempotency_key


def test_persist_proposal_skips_duplicate_key(db_session):
    """同一幂等键重复持久化应跳过，且不抛错、只留一条记录。"""
    writer = MemoryWriteService(db_session)
    gate = GateDecision(decision="accept", reason="test")

    p = _make_proposal("用户喜欢咖啡")
    p.compute_request_idempotency()
    key = p.idempotency_key

    writer._persist_proposal(p, gate, final_op="create", final_memory_id="m1")
    writer._persist_proposal(p, gate, final_op="create", final_memory_id="m1")

    rows = db_session.query(MemoryProposalModel).filter_by(idempotency_key=key).all()
    assert len(rows) == 1


def test_persist_proposal_allows_distinct_keys(db_session):
    """不同键（不同内容）应各自插入，不再误冲突。"""
    writer = MemoryWriteService(db_session)
    gate = GateDecision(decision="accept", reason="test")

    p1 = _make_proposal("用户喜欢咖啡")
    p2 = _make_proposal("用户喜欢茶")
    p1.compute_request_idempotency()
    p2.compute_request_idempotency()
    assert p1.idempotency_key != p2.idempotency_key

    writer._persist_proposal(p1, gate, final_op="create", final_memory_id="m1")
    writer._persist_proposal(p2, gate, final_op="create", final_memory_id="m2")

    assert db_session.query(MemoryProposalModel).count() == 2
