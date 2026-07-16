"""Phase 3: Segment 压缩的不可变快照构造、source_hash、item_ref 映射与确定性合并。

职责边界：
- 冻结阶段（freeze_segment_for_sealing）：构造 CompactionInput（turn_manifest /
  event_manifest / WorkingState boundary snapshot），计算 source_hash。
- Phase B：提供 item_ref → tool_call_id 局部映射，LLM 仅回传 item_ref 与语义文本。
- 合并阶段：将 LLM 语义输出与 boundary snapshot 确定性字段合并为最终 SegmentSummary。
- 覆盖校验：提交前对 boundary snapshot 执行稳定键比对，不依赖第二个 LLM。

所有 hash 均基于 canonical JSON（sort_keys），保证可重算与幂等。
"""
from __future__ import annotations

import hashlib
import json as _json
import logging
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import CompactionInput, Event, TurnRecord, WorkingState
from aiive.runtime.token_counter import LiteLLMTokenCounter
from aiive.runtime.token_models import ModelProfile, TokenSafetyConfig

logger = logging.getLogger(__name__)

# boundary WorkingState 快照中包含的字段（确定性，不含实时变化）
_WS_SNAPSHOT_FIELDS = (
    "current_objective",
    "open_loops",
    "active_constraints",
    "pending_approvals",
    "artifact_refs",
    "verified_tool_states",
    "uncommitted_side_effects",
    "running_tool_state",
    "version",
    "epoch_id",
)


# ============================================================================
# 内容 hash 与 manifest
# ============================================================================


def event_content_hash(event: Event) -> str:
    """单个 Event 的确定性 content_hash（基于 event_type + payload）。"""
    canonical = _json.dumps(
        {"event_type": event.event_type, "payload": event.payload},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def working_state_snapshot_hash(snapshot: dict[str, Any]) -> str:
    """boundary WorkingState 快照的 hash。"""
    canonical = _json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_turn_manifest(db: Session, segment_id: str) -> list[dict[str, Any]]:
    """Segment 内所有 TurnRecord 的有序清单。"""
    turns = (
        db.query(TurnRecord)
        .filter(TurnRecord.segment_id == segment_id)
        .order_by(TurnRecord.turn_sequence)
        .all()
    )
    return [
        {
            "turn_record_id": t.id,
            "turn_id": t.turn_id,
            "turn_sequence": t.turn_sequence,
            "status": t.status,
        }
        for t in turns
    ]


def build_event_manifest(db: Session, turn_ids: list[str]) -> list[dict[str, Any]]:
    """Segment 内所有 Event 的有序清单（含 content_hash）。

    turn_record_id 通过 turn_id 关联回 TurnRecord，供 Phase B 逐条校验。
    """
    if not turn_ids:
        return []
    tr_map = {
        t.turn_id: t.id
        for t in db.query(TurnRecord).filter(TurnRecord.turn_id.in_(turn_ids)).all()
    }
    events = (
        db.query(Event)
        .filter(Event.turn_id.in_(turn_ids))
        .order_by(Event.turn_id, Event.turn_event_index)
        .all()
    )
    return [
        {
            "event_id": e.id,
            "turn_record_id": tr_map.get(e.turn_id) if e.turn_id else None,
            "turn_event_index": e.turn_event_index,
            "event_type": e.event_type,
            "content_hash": event_content_hash(e),
        }
        for e in events
    ]


def compute_source_hash(
    *,
    thread_id: str,
    epoch_id: str,
    segment_id: str,
    start_turn_sequence: int,
    end_turn_sequence: int,
    turn_manifest: list[dict[str, Any]],
    event_manifest: list[dict[str, Any]],
    ws_snapshot_hash: str,
    summary_version: int,
) -> str:
    """基于 turn_manifest / event_manifest / WS 快照的 canonical source_hash。

    禁止仅依赖 thread_id/序列号（见 Phase_3.md E.3.1）。
    """
    canonical = _json.dumps(
        {
            "thread_id": thread_id,
            "epoch_id": epoch_id,
            "segment_id": segment_id,
            "start_turn_sequence": start_turn_sequence,
            "end_turn_sequence": end_turn_sequence,
            "turn_manifest": sorted(
                turn_manifest,
                key=lambda m: (m["turn_sequence"], m["turn_record_id"]),
            ),
            "event_manifest": sorted(
                event_manifest,
                key=lambda m: (m["turn_record_id"] or "", m["turn_event_index"] or 0),
            ),
            "working_state_snapshot_hash": ws_snapshot_hash,
            "summary_version": summary_version,
        },
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_working_state_snapshot(ws: WorkingState) -> dict[str, Any]:
    """从 WorkingState 提取不可变 boundary 快照。"""
    return {field: getattr(ws, field) for field in _WS_SNAPSHOT_FIELDS if hasattr(ws, field)}


# ============================================================================
# CompactionInput 冻结
# ============================================================================


def freeze_compaction_input(
    db: Session,
    *,
    segment_id: str,
    thread_id: str,
    epoch_id: str,
    start_turn_sequence: int,
    end_turn_sequence: int,
    boundary_working_state: WorkingState | None,
    summary_version: int = 1,
) -> tuple[CompactionInput, str]:
    """构造不可变 CompactionInput 并计算 source_hash。

    返回 (CompactionInput, source_hash)。调用方负责在同一事务内 add/flush。
    """
    turn_manifest = build_turn_manifest(db, segment_id)
    turn_ids = [m["turn_id"] for m in turn_manifest]
    event_manifest = build_event_manifest(db, turn_ids)

    ws_snapshot = build_working_state_snapshot(boundary_working_state) if boundary_working_state is not None else {}
    ws_snap_hash = working_state_snapshot_hash(ws_snapshot)

    source_hash = compute_source_hash(
        thread_id=thread_id,
        epoch_id=epoch_id,
        segment_id=segment_id,
        start_turn_sequence=start_turn_sequence,
        end_turn_sequence=end_turn_sequence,
        turn_manifest=turn_manifest,
        event_manifest=event_manifest,
        ws_snapshot_hash=ws_snap_hash,
        summary_version=summary_version,
    )

    ci = CompactionInput(
        segment_id=segment_id,
        start_turn_sequence=start_turn_sequence,
        end_turn_sequence=end_turn_sequence,
        turn_manifest=turn_manifest,
        event_manifest=event_manifest,
        working_state_version=ws_snapshot.get("version", 0) or 0,
        working_state_snapshot=ws_snapshot,
        source_hash=source_hash,
        summary_version=summary_version,
    )
    return ci, source_hash


# ============================================================================
# item_ref 局部引用映射（Item 4）
# ============================================================================


def build_item_ref_map(
    verified_tool_states: list[dict[str, Any]],
    unresolved_failures: list[dict[str, Any]],
) -> dict[str, Any]:
    """生成局部 item_ref → tool_call_id 映射，供 Prompt 与回填空映射使用。

    verified_tool_states 每项含顶层 tool_call_id（见 working_state 提升）。
    unresolved_failures 每项含 tool_call_id。
    返回：
        tool_items:      [{item_ref, tool_call_id, tool_name, state}] 供 Prompt 展示
        failure_items:   [{item_ref, tool_call_id, tool_name, error}] 供 Prompt 展示
        ref_to_tool_call_id: {item_ref: tool_call_id} 回填映射
    """
    tool_items: list[dict[str, Any]] = []
    failure_items: list[dict[str, Any]] = []
    ref_to_tool_call_id: dict[str, str] = {}

    for i, st in enumerate(verified_tool_states, start=1):
        tc = st.get("tool_call_id")
        ref = f"tool_{i}"
        tool_items.append({
            "item_ref": ref,
            "tool_call_id": tc,
            "tool_name": st.get("tool_name"),
            "state": st.get("state"),
        })
        if tc is not None:
            ref_to_tool_call_id[ref] = tc

    for j, fail in enumerate(unresolved_failures, start=1):
        tc = fail.get("tool_call_id")
        ref = f"failure_{j}"
        failure_items.append({
            "item_ref": ref,
            "tool_call_id": tc,
            "tool_name": fail.get("tool_name"),
            "error": fail.get("error"),
        })
        if tc is not None:
            ref_to_tool_call_id[ref] = tc

    return {
        "tool_items": tool_items,
        "failure_items": failure_items,
        "ref_to_tool_call_id": ref_to_tool_call_id,
    }


# ============================================================================
# LLM 语义输出校验与确定性合并（Item 4）
# ============================================================================


def validate_llm_semantic_output(llm_output: dict[str, Any], valid_item_refs: set[str]) -> list[str]:
    """校验 LLM 语义输出。

    仅允许 goal/outcome/decisions/entities/tool_result_summaries/failure_explanations；
    item_ref 必须属于 valid_item_refs 集合，禁止出现真实稳定 ID。
    返回告警列表（未知/重复 item_ref），校验失败以异常抛出。
    """
    required = {"goal", "outcome", "decisions", "entities",
                "tool_result_summaries", "failure_explanations"}
    forbidden_keys = {"open_loops", "active_constraints", "artifacts",
                      "omitted_artifact_refs", "important_tool_results",
                      "unresolved_failures"}
    warnings: list[str] = []

    missing = required - set(llm_output.keys())
    if missing:
        raise ValueError(f"LLM 输出缺少必填键: {sorted(missing)}")
    leaked = forbidden_keys & set(llm_output.keys())
    if leaked:
        raise ValueError(f"LLM 输出包含禁止的稳定字段: {sorted(leaked)}")

    seen: set[str] = set()
    for entry in llm_output.get("tool_result_summaries", []):
        ref = entry.get("item_ref")
        if ref not in valid_item_refs:
            warnings.append(f"未知 item_ref（已丢弃）: {ref}")
            continue
        if ref in seen:
            warnings.append(f"重复 item_ref（仅保留首条）: {ref}")
            continue
        seen.add(ref)

    seen_fail: set[str] = set()
    for entry in llm_output.get("failure_explanations", []):
        ref = entry.get("item_ref")
        if ref not in valid_item_refs:
            warnings.append(f"未知 item_ref（已丢弃）: {ref}")
            continue
        if ref in seen_fail:
            warnings.append(f"重复 item_ref（仅保留首条）: {ref}")
            continue
        seen_fail.add(ref)

    return warnings


def merge_summary(
    llm_output: dict[str, Any],
    *,
    boundary_snapshot: dict[str, Any],
    verified_tool_states: list[dict[str, Any]],
    unresolved_failures: list[dict[str, Any]],
    item_ref_map: dict[str, Any],
    source_turn_ids: list[str],
    source_event_ids: list[str],
    source_hash: str,
    summary_version: int,
    model_id: str | None,
    token_count: int,
) -> dict[str, Any]:
    """将 LLM 语义输出与 boundary snapshot 确定性字段合并为最终 SegmentSummary 载荷。

    工具摘要/失败说明通过 item_ref → tool_call_id 映射回填，与 LLM 输出顺序无关。
    """
    ref_to_tc = item_ref_map["ref_to_tool_call_id"]

    # LLM 回传按 item_ref 聚合
    llm_tool_by_ref = {
        e["item_ref"]: e for e in llm_output.get("tool_result_summaries", [])
        if e.get("item_ref") in ref_to_tc
    }
    llm_fail_by_ref = {
        e["item_ref"]: e for e in llm_output.get("failure_explanations", [])
        if e.get("item_ref") in ref_to_tc
    }

    important_tool_results: list[dict[str, Any]] = []
    for st in verified_tool_states:
        tc = st.get("tool_call_id")
        entry = {
            "tool_call_id": tc,
            "tool_name": st.get("tool_name"),
            "state": st.get("state"),
        }
        if tc is not None:
            ref = _find_ref_by_tc(item_ref_map, tc)
            llm_entry = llm_tool_by_ref.get(ref)
            if llm_entry:
                entry["result_summary"] = llm_entry.get("result_summary")
        important_tool_results.append(entry)

    important_failures: list[dict[str, Any]] = []
    for f in unresolved_failures:
        tc = f.get("tool_call_id")
        entry = {
            "tool_call_id": tc,
            "tool_name": f.get("tool_name"),
            "error": f.get("error"),
        }
        if tc is not None:
            ref = _find_ref_by_tc(item_ref_map, tc)
            llm_entry = llm_fail_by_ref.get(ref)
            if llm_entry:
                entry["error"] = llm_entry.get("error", f.get("error"))
        important_failures.append(entry)

    return {
        "goal": llm_output.get("goal"),
        "outcome": llm_output.get("outcome"),
        "decisions": llm_output.get("decisions", []),
        "entities": llm_output.get("entities", []),
        "open_loops": boundary_snapshot.get("open_loops", []),
        "active_constraints": boundary_snapshot.get("active_constraints", []),
        "artifacts": boundary_snapshot.get("artifact_refs", []),
        "omitted_artifact_refs": [],
        "important_tool_results": important_tool_results,
        "unresolved_failures": important_failures,
        "source_turn_ids": source_turn_ids,
        "source_event_ids": source_event_ids,
        "source_hash": source_hash,
        "summary_version": summary_version,
        "model_id": model_id,
        "token_count": token_count,
    }


def _find_ref_by_tc(item_ref_map: dict[str, Any], tool_call_id: str) -> str | None:
    for ref, tc in item_ref_map["ref_to_tool_call_id"].items():
        if tc == tool_call_id:
            return ref
    return None


def count_summary_tokens(model: str, summary_text: str) -> int:
    """使用 Phase 1 TokenCounter 计算摘要 token 数（不依赖 LLM 自报）。"""
    counter = LiteLLMTokenCounter(
        safety=TokenSafetyConfig(),
        profile=ModelProfile.from_config("deepseek", model),
    )
    tc = counter.count_messages(model, [{"role": "assistant", "content": summary_text}])
    return tc.estimated_tokens


# ============================================================================
# 失败 Turn → unresolved_failures 派生（Item 2）
# ============================================================================

# 阻断密封的非终态 Turn
NON_TERMINAL_TURN_STATUSES = {"not_started", "running", "interrupted_unknown"}
# 视为“失败/已取消”的终态 Turn
FAILED_TURN_STATUSES = {"failed", "cancelled", "preempted"}


def derive_unresolved_failures(db: Session, segment_id: str) -> list[dict[str, Any]]:
    """从 Segment 内 failed/cancelled/preempted Turn 的 tool_result 事件派生 unresolved_failures。

    仅含失败工具调用（status != completed），按 tool_call_id 去重。
    """
    turns = (
        db.query(TurnRecord)
        .filter(TurnRecord.segment_id == segment_id)
        .all()
    )
    failed_turn_ids = [
        t.turn_id for t in turns if t.status in FAILED_TURN_STATUSES
    ]
    if not failed_turn_ids:
        return []

    events = (
        db.query(Event)
        .filter(Event.turn_id.in_(failed_turn_ids), Event.event_type == "tool_result")
        .all()
    )
    failures: list[dict[str, Any]] = []
    for e in events:
        payload = e.payload or {}
        status = payload.get("status")
        if status == "completed":
            continue
        failures.append({
            "tool_call_id": payload.get("tool_call_id"),
            "tool_name": payload.get("name"),
            "error": _json.dumps(payload, ensure_ascii=False, default=str)[:500],
        })
    seen: set[str] = set()
    dedup: list[dict[str, Any]] = []
    for f in failures:
        tc = f.get("tool_call_id")
        if tc is None or tc in seen:
            continue
        seen.add(tc)
        dedup.append(f)
    return dedup
