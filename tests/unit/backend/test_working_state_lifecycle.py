"""Phase 1: WorkingStateService 确定性字段生命周期单元测试。

验证各确定性字段（running_tool_state / verified_tool_states /
artifact_refs / uncommitted_side_effects / pending_approvals）的
增删、容量上限与版本自增。这些字段由 Turn 生命周期维护，
是缺口 B 接线的基础单元。
"""
import pytest

from aiive.db.models import WorkingState
from aiive.runtime.working_state import MAX_LIST_ITEMS, WorkingStateService


def _svc() -> WorkingStateService:
    return WorkingStateService()


def test_get_or_create(db_session):
    ws = _svc().get_or_create(db_session, "t1")
    assert ws.thread_id == "t1"
    db_session.commit()
    assert db_session.query(WorkingState).filter(WorkingState.thread_id == "t1").count() == 1


def test_running_tool_lifecycle(db_session):
    svc = _svc()
    svc.add_running_tool(db_session, "t1", "tr1", "e1", "tc1", "tool_a")
    db_session.commit()
    row = db_session.query(WorkingState).filter(WorkingState.thread_id == "t1").first()
    assert len(row.running_tool_state) == 1

    svc.remove_running_tool(db_session, "t1", "tc1")
    db_session.commit()
    assert db_session.query(WorkingState).filter(WorkingState.thread_id == "t1").first().running_tool_state == []


def test_verified_and_artifact_refs(db_session):
    svc = _svc()
    svc.update_verified_tool_state(db_session, "t1", "tool_a", {"ok": True})
    svc.add_artifact_ref(db_session, "t1", "artifact://abc", "tool_result", "tool_a")
    db_session.commit()
    row = db_session.query(WorkingState).filter(WorkingState.thread_id == "t1").first()
    assert any(s["tool_name"] == "tool_a" for s in row.verified_tool_states)
    assert any(a["ref"] == "artifact://abc" for a in row.artifact_refs)


def test_side_effect_and_approval(db_session):
    svc = _svc()
    svc.add_uncommitted_side_effect(db_session, "t1", "ref1", "desc", "high")
    svc.add_pending_approval(db_session, "t1", "ap1", "do something")
    db_session.commit()
    row = db_session.query(WorkingState).filter(WorkingState.thread_id == "t1").first()
    assert row.uncommitted_side_effects[0]["ref"] == "ref1"
    assert row.pending_approvals[0]["id"] == "ap1"

    svc.remove_uncommitted_side_effect(db_session, "t1", "ref1")
    svc.remove_pending_approval(db_session, "t1", "ap1")
    db_session.commit()
    row = db_session.query(WorkingState).filter(WorkingState.thread_id == "t1").first()
    assert row.uncommitted_side_effects == []
    assert row.pending_approvals == []


def test_list_cap_enforced(db_session):
    svc = _svc()
    for i in range(MAX_LIST_ITEMS + 5):
        svc.add_artifact_ref(db_session, "t1", f"artifact://{i}", "tool_result", "t")
    db_session.commit()
    row = db_session.query(WorkingState).filter(WorkingState.thread_id == "t1").first()
    assert len(row.artifact_refs) == MAX_LIST_ITEMS


def test_version_increments_on_write(db_session):
    svc = _svc()
    before = svc.get_or_create(db_session, "t1").version
    svc.add_artifact_ref(db_session, "t1", "artifact://x", "tool_result", "t")
    db_session.commit()
    after = db_session.query(WorkingState).filter(WorkingState.thread_id == "t1").first().version
    assert after == before + 1
