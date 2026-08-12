"""Phase 3: compaction 纯函数单元测试（测试矩阵 11/12/13/52/59/60/68/70/71/72）。"""
from __future__ import annotations

import pytest

from aiive.runtime.compaction import (
    build_item_ref_map,
    compute_source_hash,
    derive_unresolved_failures,
    event_content_hash,
    freeze_compaction_input,
    merge_summary,
    validate_llm_semantic_output,
)
from aiive.worker.outbox_handlers import _run_cover_checks
from backend.tests._util import add_event, add_turn, new_epoch, new_segment, new_thread, new_ws


def _freeze(db, seg, thread, epoch, ws, **kw):
    ci, source_hash = freeze_compaction_input(
        db, segment_id=seg.id, thread_id=thread.id, epoch_id=epoch.id,
        start_turn_sequence=seg.start_turn_sequence or 0,
        end_turn_sequence=1, boundary_working_state=ws, **kw,
    )
    db.add(ci)
    db.flush()
    return ci, source_hash


def test_70_manifest_fully_frozen(db):
    """turn_manifest / event_manifest 被完整冻结（含 content_hash、event_type、turn_event_index）。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    t = add_turn(db, thread.id, seg.id, 1)
    add_event(db, thread.id, t.turn_id, "user_message", {"content": "hi"}, 0)
    add_event(db, thread.id, t.turn_id, "tool_result",
              {"name": "x", "tool_call_id": "tc1", "status": "completed"}, 1)
    ws = new_ws(db, thread.id)
    db.commit()

    ci, _ = _freeze(db, seg, thread, epoch, ws)
    assert ci.turn_manifest[0]["turn_record_id"] == t.id
    assert ci.turn_manifest[0]["turn_sequence"] == 1
    assert ci.turn_manifest[0]["status"] == "completed"
    assert ci.event_manifest[0]["event_type"] == "user_message"
    assert "content_hash" in ci.event_manifest[0]
    assert ci.event_manifest[1]["turn_event_index"] == 1


def test_13_tool_events_in_manifest(db):
    """tool events 被纳入 source manifest。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    t = add_turn(db, thread.id, seg.id, 1)
    add_event(db, thread.id, t.turn_id, "tool_call",
              {"name": "search", "tool_call_id": "tc1"}, 0)
    add_event(db, thread.id, t.turn_id, "tool_result",
              {"name": "search", "tool_call_id": "tc1", "status": "completed"}, 1)
    ws = new_ws(db, thread.id)
    db.commit()

    from aiive.runtime.compaction import build_event_manifest
    manifest = build_event_manifest(db, [t.turn_id])
    types = {m["event_type"] for m in manifest}
    assert {"tool_call", "tool_result"} <= types


def test_11_freeze_captures_ws_snapshot_immutably(db):
    """CompactionInput 冻结后 WS 新变化不影响已冻结快照。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    add_turn(db, thread.id, seg.id, 1)
    ws = new_ws(db, thread.id, current_objective="old")
    db.commit()

    ci, _ = _freeze(db, seg, thread, epoch, ws)
    assert ci.working_state_snapshot["current_objective"] == "old"

    ws.current_objective = "new"
    db.commit()
    db.expire_all()
    ci2 = db.get(type(ci), ci.id)
    assert ci2.working_state_snapshot["current_objective"] == "old"


def test_12_event_content_change_changes_source_hash(db):
    """Event 内容变化 → source_hash 不一致（不可提交）。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    t = add_turn(db, thread.id, seg.id, 1)
    ev = add_event(db, thread.id, t.turn_id, "user_message", {"content": "hi"}, 0)
    ws = new_ws(db, thread.id)
    db.commit()

    ci, source_hash = _freeze(db, seg, thread, epoch, ws)
    assert source_hash == ci.source_hash

    # 内容变化后，重算 hash 必须不同
    ev.payload = {"content": "changed"}
    db.commit()
    db.expire_all()
    new_hash = event_content_hash(db.get(type(ev), ev.id))
    assert new_hash != ci.event_manifest[0]["content_hash"]


def test_52_omitted_artifact_refs_persistable(db):
    """omitted_artifact_refs 可持久化并参与 checker。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    add_turn(db, thread.id, seg.id, 1)
    ws = new_ws(db, thread.id)
    db.commit()

    verified = [{"tool_call_id": "tc1", "tool_name": "x", "state": {"ok": True}}]
    item_ref_map = build_item_ref_map(verified, [])
    llm = {
        "goal": "g", "outcome": "o", "decisions": [], "entities": [],
        "tool_result_summaries": [{"item_ref": "tool_1", "result_summary": "ok"}],
        "failure_explanations": [],
    }
    summary = merge_summary(
        llm, boundary_snapshot=ws.__dict__, verified_tool_states=verified,
        unresolved_failures=[], item_ref_map=item_ref_map,
        source_turn_ids=["t1"], source_event_ids=["e1"],
        source_hash="h", summary_version=1, model_id="m", token_count=1,
    )
    assert summary["omitted_artifact_refs"] == []


def test_59_llm_no_boundary_stable_fields_and_deterministic_merge(db):
    """LLM 输出不含 boundary 稳定字段；LLM 遗漏 open_loop/artifact 时代码确定性合并仍完整。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    add_turn(db, thread.id, seg.id, 1)
    ws = new_ws(db, thread.id,
                current_objective="obj",
                open_loops=[{"description": "loop1"}],
                active_constraints=[{"description": "c1"}],
                artifact_refs=[{"ref": "a1"}])
    db.commit()

    verified = [{"tool_call_id": "tc1", "tool_name": "x", "state": {"ok": True}}]
    item_ref_map = build_item_ref_map(verified, [])
    valid_refs = set(item_ref_map["ref_to_tool_call_id"].keys())

    # 禁止 LLM 输出 boundary 稳定字段
    with pytest.raises(ValueError):
        validate_llm_semantic_output({
            "goal": "g", "outcome": "o", "decisions": [], "entities": [],
            "tool_result_summaries": [], "failure_explanations": [],
            "open_loops": [{"description": "hack"}],  # 禁止键
        }, valid_refs)

    # 合法输出：不含稳定字段，但合并后 boundary 字段仍由代码补全
    llm = {
        "goal": "g", "outcome": "o", "decisions": [], "entities": [],
        "tool_result_summaries": [{"item_ref": "tool_1", "result_summary": "ok"}],
        "failure_explanations": [],
    }
    snapshot = {f: getattr(ws, f) for f in (
        "current_objective", "open_loops", "active_constraints",
        "pending_approvals", "artifact_refs", "verified_tool_states",
        "uncommitted_side_effects", "running_tool_state", "version", "epoch_id",
    ) if hasattr(ws, f)}
    summary = merge_summary(
        llm, boundary_snapshot=snapshot, verified_tool_states=verified,
        unresolved_failures=[], item_ref_map=item_ref_map,
        source_turn_ids=["t1"], source_event_ids=["e1"],
        source_hash="h", summary_version=1, model_id="m", token_count=1,
    )
    assert summary["open_loops"] == [{"description": "loop1"}]
    assert summary["active_constraints"] == [{"description": "c1"}]
    assert summary["artifacts"] == [{"ref": "a1"}]


def test_60_checker_uses_real_tool_call_id(db):
    """checker 只使用真实 verified-tool 稳定键（tool_call_id），不依赖源 WS 中不存在的 ID。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    add_turn(db, thread.id, seg.id, 1)
    ws = new_ws(db, thread.id)
    db.commit()

    verified = [{"tool_call_id": "tc1", "tool_name": "x", "state": {"ok": True}}]
    snapshot = {f: getattr(ws, f) for f in (
        "current_objective", "open_loops", "active_constraints",
        "pending_approvals", "artifact_refs", "verified_tool_states",
        "uncommitted_side_effects", "running_tool_state", "version", "epoch_id",
    ) if hasattr(ws, f)}
    snapshot["verified_tool_states"] = verified

    fake_ci = type("CI", (), {"working_state_snapshot": snapshot,
                               "turn_manifest": [{"turn_id": "t1"}]})()

    # 匹配：summary 含 tc1 → 无违规
    ok_payload = {
        "open_loops": [], "active_constraints": [], "artifacts": [],
        "important_tool_results": [{"tool_call_id": "tc1"}],
        "source_turn_ids": ["t1"],
    }
    assert _run_cover_checks(fake_ci, ok_payload) == []

    # 缺失 tc1 → 覆盖校验报告 verified_tool_states 未完整保留
    bad_payload = {
        "open_loops": [], "active_constraints": [], "artifacts": [],
        "important_tool_results": [],
        "source_turn_ids": ["t1"],
    }
    violations = _run_cover_checks(fake_ci, bad_payload)
    assert any("verified_tool_states" in v for v in violations)


def test_68_verified_tool_states_top_level_tool_call_id(db):
    """verified_tool_states 条目 tool_call_id 提升为顶层字段且 item_ref 映射按其对齐。"""
    verified = [
        {"tool_call_id": "tc1", "tool_name": "search", "state": {"ok": True}},
        {"tool_call_id": "tc2", "tool_name": "calc", "state": {"ok": True}},
    ]
    item_ref_map = build_item_ref_map(verified, [])
    assert item_ref_map["ref_to_tool_call_id"] == {"tool_1": "tc1", "tool_2": "tc2"}
    assert item_ref_map["tool_items"][0]["tool_call_id"] == "tc1"
    assert item_ref_map["tool_items"][1]["tool_call_id"] == "tc2"


def test_71_shuffled_item_ref_order_backfill_correct(db):
    """LLM 打乱 tool_result_summaries 顺序，回填仍按 item_ref→tool_call_id 正确映射。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    add_turn(db, thread.id, seg.id, 1)
    ws = new_ws(db, thread.id)
    db.commit()

    verified = [
        {"tool_call_id": "tc1", "tool_name": "search", "state": {"ok": True}},
        {"tool_call_id": "tc2", "tool_name": "calc", "state": {"ok": True}},
    ]
    item_ref_map = build_item_ref_map(verified, [])
    # 故意逆序
    llm = {
        "goal": "g", "outcome": "o", "decisions": [], "entities": [],
        "tool_result_summaries": [
            {"item_ref": "tool_2", "result_summary": "calc-ok"},
            {"item_ref": "tool_1", "result_summary": "search-ok"},
        ],
        "failure_explanations": [],
    }
    summary = merge_summary(
        llm, boundary_snapshot={}, verified_tool_states=verified,
        unresolved_failures=[], item_ref_map=item_ref_map,
        source_turn_ids=[], source_event_ids=[],
        source_hash="h", summary_version=1, model_id="m", token_count=1,
    )
    tools = {t["tool_call_id"]: t.get("result_summary") for t in summary["important_tool_results"]}
    assert tools["tc1"] == "search-ok"
    assert tools["tc2"] == "calc-ok"


def test_72_unknown_item_ref_dropped_not_overwriting(db):
    """LLM 回传未知/编造 item_ref 不覆盖确定性工具状态，仅告警丢弃。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    add_turn(db, thread.id, seg.id, 1)
    ws = new_ws(db, thread.id)
    db.commit()

    verified = [{"tool_call_id": "tc1", "tool_name": "search", "state": {"ok": True}}]
    item_ref_map = build_item_ref_map(verified, [])
    valid_refs = set(item_ref_map["ref_to_tool_call_id"].keys())

    llm = {
        "goal": "g", "outcome": "o", "decisions": [], "entities": [],
        "tool_result_summaries": [
            {"item_ref": "tool_999", "result_summary": "fake"},  # 未知
            {"item_ref": "tool_1", "result_summary": "real-ok"},
        ],
        "failure_explanations": [],
    }
    warnings = validate_llm_semantic_output(llm, valid_refs)
    assert any("tool_999" in w or "未知" in w for w in warnings)

    summary = merge_summary(
        llm, boundary_snapshot={}, verified_tool_states=verified,
        unresolved_failures=[], item_ref_map=item_ref_map,
        source_turn_ids=[], source_event_ids=[],
        source_hash="h", summary_version=1, model_id="m", token_count=1,
    )
    tools = {t["tool_call_id"]: t.get("result_summary") for t in summary["important_tool_results"]}
    assert tools["tc1"] == "real-ok"
    assert "tool_999" not in str(summary["important_tool_results"])
