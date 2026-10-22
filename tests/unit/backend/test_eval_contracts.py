"""Kernel Contract Checks: positive + necessary negative tests."""
from aiive.db.models import ContextSnapshot, OutboxJob, TurnRecord
from aiive.eval.contracts import (
    check_context_snapshot_persisted,
    check_key_events_persisted,
    check_memory_single_source,
    check_outbox_idempotent,
    check_safe_delete_guard,
    run_contract_checks,
)
from aiive.memory.memory_store import MemoryStore
from aiive.memory.memory_types import (
    EvidenceItem,
    LifecycleState,
    MemoryProposal,
    TrustLevel,
)


def _make_proposal(key, content):
    p = MemoryProposal(
        memory_type="user_profile", canonical_key=key, content=content,
        evidence=[EvidenceItem(source_type="user_message", trust_level=TrustLevel.TRUSTED.value)],
    )
    p.compute_request_idempotency()
    return p


def test_all_contracts_pass_on_clean_db(db_session):
    results = run_contract_checks(db_session)
    assert results, "expected contract results"
    failed = [c for c in results if not c.ok]
    assert not failed, [c.to_dict() if hasattr(c, "to_dict") else (c.name, c.detail) for c in failed]


def test_memory_single_source_negative(db_session):
    # 注册为 single cardinality 的精确键（memory_key_registry）不得有多条 active 记录
    store = MemoryStore(db_session)
    store.create_record(_make_proposal("user.display_name", "v1"), lifecycle_state=LifecycleState.ACTIVE.value)
    store.create_record(_make_proposal("user.display_name", "v2"), lifecycle_state=LifecycleState.ACTIVE.value)
    db_session.flush()
    res = check_memory_single_source(db_session)
    assert res.ok is False
    assert "user.display_name" in res.evidence["duplicate_keys"]


def test_memory_single_source_multi_cardinality_is_legal(db_session):
    # multi cardinality 模式键（project.*，conflict_policy=append）多条 active 合法
    store = MemoryStore(db_session)
    store.create_record(_make_proposal("project.eval.dup", "v1"), lifecycle_state=LifecycleState.ACTIVE.value)
    store.create_record(_make_proposal("project.eval.dup", "v2"), lifecycle_state=LifecycleState.ACTIVE.value)
    db_session.flush()
    res = check_memory_single_source(db_session)
    assert res.ok is True, res.detail
    assert res.evidence["multi_cardinality_keys"] == {"project.eval.dup": 2}


def test_key_events_persisted_negative(db_session):
    turn = TurnRecord(id="t1", thread_id="th1", turn_id="tu1", status="completed")
    db_session.add(turn)
    db_session.flush()
    res = check_key_events_persisted(db_session)
    assert res.ok is False
    assert "tu1" in res.evidence["turns_missing_terminal_event"]


def test_outbox_idempotent_probe_and_duplicate_rejection(db_session):
    import pytest
    from sqlalchemy.exc import IntegrityError


    # (a) the contract's idempotency probe must hold (no duplicate persisted).
    res = check_outbox_idempotent(db_session)
    assert res.ok is True, res.detail

    # (b) reverse: the unique operation_id constraint enforces idempotency.
    #     re-inserting the same operation_id is rejected by the DB.
    db_session.add(OutboxJob(operation_id="dup-op", job_type="memory_extraction", status="pending"))
    db_session.flush()
    db_session.add(OutboxJob(operation_id="dup-op", job_type="memory_extraction", status="pending"))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_safe_delete_guard_positive(db_session):
    res = check_safe_delete_guard(db_session)
    assert res.ok is True, res.detail
    assert res.evidence["in_scope_allowed"] is True
    assert res.evidence["dangerous_allowed"] is False


def test_safe_delete_guard_negative(monkeypatch, db_session):

    def fake_allowed(*args, **kwargs):
        class _D:
            allowed = True
            reason = "patched"
        return _D()

    monkeypatch.setattr("aiive.tools.safe_delete.safe_delete", fake_allowed)
    res = check_safe_delete_guard(db_session)
    assert res.ok is False


def test_context_snapshot_persisted_negative(db_session):
    turn = TurnRecord(id="t2", thread_id="th2", turn_id="tu2", status="completed")
    db_session.add(turn)
    db_session.flush()
    # no snapshot for th2 -> must fail
    res = check_context_snapshot_persisted(db_session)
    assert res.ok is False
    assert "tu2" in res.evidence["turns_missing_snapshot"]


def test_context_snapshot_persisted_positive_with_snapshot(db_session):
    turn = TurnRecord(id="t3", thread_id="th3", turn_id="tu3", status="completed")
    db_session.add(turn)
    db_session.add(ContextSnapshot(id="cs1", trace_id="tr", thread_id="th3",
                                   stable_prefix_hash="x", context_items=[], meta={}))
    db_session.flush()
    res = check_context_snapshot_persisted(db_session)
    assert res.ok is True
