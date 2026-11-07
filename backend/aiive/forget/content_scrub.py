"""Forget 内容清理与逐表验证。

Scrub 与 Verify 共用 `ContentScope`；所有 JSON 数组成员判断走
`content_scope.select_json_array_members`（PG jsonb / SQLite json_each），不用 LIKE。

对象存储：数据库事务内只解除引用并登记 `forget_actions`（pending）；COMMIT 之后
再删除对象。删除失败保留 failed 动作，由 Purge 重试；Verify 会检查动作状态与对象
是否真实不存在。
"""
# pyright: reportImplicitStringConcatenation=false
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from collections.abc import Iterable
from typing import Any

from sqlalchemy import bindparam, text as sa_text
from sqlalchemy.orm import Session

from aiive.db.forget_models import ForgetAction
from aiive.db.models import (
    AgentTask,
    Document,
    OutboxJob,
    TaskArtifact,
    TaskEvidence,
    Thread,
)
from aiive.worker.outbox_payload_contract import (
    is_internal_job,
    payload_has_content,
    redact_job_payload,
)
from aiive.forget.content_scope import (
    ContentScope,
    count_json_array_members,
    document_chunk_ids,
    select_json_array_members,
)
from aiive.storage import object_store

logger = logging.getLogger(__name__)

_CHUNK = 400
_REDACTED = "[redacted]"
_FORGOTTEN = "[forgotten]"


def _chunks(items: Iterable[str]) -> list[list[str]]:
    values = list(dict.fromkeys(str(i) for i in items if i))
    return [values[i:i + _CHUNK] for i in range(0, len(values), _CHUNK)]


def _execute_ids(
    db: Session, sql: str, ids: Iterable[str], params: dict[str, Any] | None = None,
) -> int:
    """按 IN :ids 分批执行，返回总 rowcount。"""
    total = 0
    base = params or {}
    for chunk in _chunks(ids):
        stmt = sa_text(sql).bindparams(bindparam("ids", expanding=True))
        result = db.execute(stmt, {**base, "ids": chunk})
        total += int(getattr(result, "rowcount", 0) or 0)
    return total


def _ids_from(db: Session, sql: str, ids: Iterable[str]) -> set[str]:
    """按 IN :ids 分批查询单列 id。"""
    out: set[str] = set()
    for chunk in _chunks(ids):
        stmt = sa_text(sql).bindparams(bindparam("ids", expanding=True))
        out |= {str(r[0]) for r in db.execute(stmt, {"ids": chunk}).all() if r[0]}
    return out


# ═══════════════════════════════════════════════════════════════════
# 受影响记录选择（Scrub / Verify 共用）
# ═══════════════════════════════════════════════════════════════════

def affected_proposal_ids(db: Session, scope: ContentScope) -> set[str]:
    ids: set[str] = set()
    if scope.all_user:
        ids |= {str(r[0]) for r in db.execute(sa_text("SELECT id FROM memory_proposals")).all() if r[0]}
    if scope.memory_ids:
        ids |= _ids_from(db, "SELECT id FROM memory_proposals WHERE final_memory_id IN :ids", scope.memory_ids)
    if scope.event_ids:
        ids |= set(select_json_array_members(
            db, "memory_proposals", "id", "source_event_ids", sorted(scope.event_ids),
        ))
    if scope.turn_tokens:
        ids |= _ids_from(
            db,
            "SELECT id FROM memory_proposals WHERE source_turn_id IN :ids "
            "OR source_turn_record_id IN :ids",
            scope.turn_tokens,
        )
    if scope.time_from is not None or scope.time_to is not None:
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if scope.time_from is not None:
            clauses.append("created_at >= :tf")
            params["tf"] = scope.time_from
        if scope.time_to is not None:
            clauses.append("created_at <= :tt")
            params["tt"] = scope.time_to
        rows = db.execute(
            sa_text("SELECT id FROM memory_proposals WHERE " + " AND ".join(clauses)),
            params,
        ).all()
        ids |= {str(r[0]) for r in rows if r[0]}
    return ids


def affected_summary_ids(db: Session, scope: ContentScope) -> set[str]:
    ids: set[str] = set()
    if scope.segment_ids:
        ids |= _ids_from(db, "SELECT id FROM segment_summaries WHERE segment_id IN :ids", scope.segment_ids)
    if scope.event_ids:
        ids |= set(select_json_array_members(
            db, "segment_summaries", "id", "source_event_ids", sorted(scope.event_ids),
        ))
    if scope.turn_tokens:
        ids |= set(select_json_array_members(
            db, "segment_summaries", "id", "source_turn_ids", sorted(scope.turn_tokens),
        ))
    return ids


def affected_checkpoint_ids(db: Session, scope: ContentScope) -> set[str]:
    ids: set[str] = set()
    if scope.epoch_ids:
        ids |= _ids_from(db, "SELECT id FROM epoch_checkpoints WHERE epoch_id IN :ids", scope.epoch_ids)
    if scope.segment_ids:
        ids |= set(select_json_array_members(
            db, "epoch_checkpoints", "id", "source_segment_ids", sorted(scope.segment_ids),
        ))
    return ids


def _thread_or_time_ids(
    db: Session, table: str, time_column: str, scope: ContentScope,
) -> set[str]:
    ids: set[str] = set()
    if scope.thread_ids:
        stmt = sa_text(
            f"SELECT id FROM {table} WHERE thread_id IN :tids AND {time_column} <= :cutoff"
        ).bindparams(bindparam("tids", expanding=True))
        for chunk in _chunks(scope.thread_ids):
            rows = db.execute(stmt, {"tids": chunk, "cutoff": scope.cutoff}).all()
            ids |= {str(r[0]) for r in rows if r[0]}
    if scope.all_user:
        rows = db.execute(
            sa_text(f"SELECT id FROM {table} WHERE {time_column} <= :cutoff"),
            {"cutoff": scope.cutoff},
        ).all()
        ids |= {str(r[0]) for r in rows if r[0]}
    if scope.all_user or scope.time_from is not None or scope.time_to is not None:
        clauses: list[str] = [f"{time_column} <= :cutoff"]
        params: dict[str, Any] = {"cutoff": scope.cutoff}
        if scope.time_from is not None:
            clauses.append(f"{time_column} >= :tf")
            params["tf"] = scope.time_from
        if scope.time_to is not None:
            clauses.append(f"{time_column} <= :tt")
            params["tt"] = scope.time_to
        rows = db.execute(
            sa_text(f"SELECT id FROM {table} WHERE " + " AND ".join(clauses)), params,
        ).all()
        ids |= {str(r[0]) for r in rows if r[0]}
    return ids


def working_state_ids(db: Session, scope: ContentScope) -> set[str]:
    return _thread_or_time_ids(db, "working_states", "updated_at", scope)


def context_snapshot_ids(db: Session, scope: ContentScope) -> set[str]:
    ids = _thread_or_time_ids(db, "context_snapshots", "created_at", scope)
    if scope.segment_ids:
        ids |= _ids_from(db, "SELECT id FROM context_snapshots WHERE segment_id IN :ids", scope.segment_ids)
    return ids


def llm_call_ids(db: Session, scope: ContentScope) -> set[str]:
    ids = _thread_or_time_ids(db, "llm_calls", "created_at", scope)
    if scope.trace_ids:
        ids |= _ids_from(
            db, "SELECT id FROM llm_calls WHERE trace_id IN :ids", scope.trace_ids,
        )
    return ids


def task_note_ids(db: Session, scope: ContentScope) -> set[str]:
    ids = _thread_or_time_ids(db, "tasks", "created_at", scope)
    if scope.event_ids:
        ids |= _ids_from(
            db, "SELECT id FROM tasks WHERE source_event_id IN :ids", scope.event_ids,
        )
    return ids


def attention_ids(db: Session, scope: ContentScope) -> set[str]:
    return _thread_or_time_ids(db, "attention_states", "created_at", scope)


def inbox_ids(db: Session, scope: ContentScope) -> set[str]:
    ids = _thread_or_time_ids(db, "turn_event_inbox", "created_at", scope) | scope.turn_event_inbox_ids
    if scope.turn_tokens:
        ids |= _ids_from(
            db,
            "SELECT id FROM turn_event_inbox WHERE turn_record_id IN :ids "
            "OR turn_id IN :ids",
            scope.turn_tokens,
        )
    if scope.event_ids:
        ids |= _ids_from(
            db, "SELECT id FROM turn_event_inbox WHERE message_event_id IN :ids",
            scope.event_ids,
        )
    return ids


def tool_operation_ids(db: Session, scope: ContentScope) -> set[str]:
    ids = _thread_or_time_ids(db, "tool_operations", "created_at", scope)
    if scope.turn_tokens:
        ids |= _ids_from(
            db, "SELECT id FROM tool_operations WHERE turn_record_id IN :ids",
            scope.turn_tokens,
        )
    return ids


def approval_request_ids(db: Session, scope: ContentScope) -> set[str]:
    ids = _thread_or_time_ids(db, "approval_requests", "created_at", scope)
    if scope.turn_tokens:
        ids |= _ids_from(
            db, "SELECT id FROM approval_requests WHERE turn_record_id IN :ids",
            scope.turn_tokens,
        )
    if scope.agent_task_ids:
        ids |= _ids_from(
            db, "SELECT id FROM approval_requests WHERE task_id IN :ids",
            scope.agent_task_ids,
        )
    if scope.agent_action_ids:
        ids |= _ids_from(
            db, "SELECT id FROM approval_requests WHERE action_id IN :ids",
            scope.agent_action_ids,
        )
    return ids


def artifact_ids(db: Session, scope: ContentScope) -> set[str]:
    ids = _thread_or_time_ids(db, "artifacts", "created_at", scope)
    if scope.turn_tokens:
        ids |= _ids_from(
            db, "SELECT id FROM artifacts WHERE turn_record_id IN :ids", scope.turn_tokens,
        )
    return ids


def compaction_input_ids(db: Session, scope: ContentScope) -> set[str]:
    if not scope.segment_ids:
        return set()
    return _ids_from(db, "SELECT id FROM compaction_inputs WHERE segment_id IN :ids", scope.segment_ids)


def epoch_compaction_input_ids(db: Session, scope: ContentScope) -> set[str]:
    if not scope.epoch_ids:
        return set()
    return _ids_from(db, "SELECT id FROM epoch_compaction_inputs WHERE epoch_id IN :ids", scope.epoch_ids)


def retrieval_entry_ids(db: Session, scope: ContentScope) -> set[str]:
    ids: set[str] = set()
    if scope.memory_ids:
        ids |= _ids_from(
            db,
            "SELECT id FROM retrieval_index_entries WHERE source_type = 'memory_record' "
            "AND source_id IN :ids",
            scope.memory_ids,
        )
    if scope.segment_ids:
        ids |= _ids_from(
            db, "SELECT id FROM retrieval_index_entries WHERE segment_id IN :ids",
            scope.segment_ids,
        )
    if scope.epoch_ids:
        ids |= _ids_from(
            db, "SELECT id FROM retrieval_index_entries WHERE epoch_id IN :ids",
            scope.epoch_ids,
        )
    summary_ids = affected_summary_ids(db, scope)
    if summary_ids:
        ids |= _ids_from(
            db,
            "SELECT id FROM retrieval_index_entries WHERE source_type = 'segment_summary' "
            "AND source_id IN :ids",
            summary_ids,
        )
    checkpoint_ids = affected_checkpoint_ids(db, scope)
    if checkpoint_ids:
        ids |= _ids_from(
            db,
            "SELECT id FROM retrieval_index_entries WHERE source_type = 'epoch_checkpoint' "
            "AND source_id IN :ids",
            checkpoint_ids,
        )
    if scope.thread_ids:
        rows = db.execute(
            sa_text(
                "SELECT id FROM retrieval_index_entries WHERE thread_id IN :tids "
                "AND (created_source_at IS NULL OR created_source_at <= :cutoff)"
            ).bindparams(bindparam("tids", expanding=True)),
            {"tids": list(scope.thread_ids), "cutoff": scope.cutoff},
        ).all()
        ids |= {str(r[0]) for r in rows if r[0]}
    clauses: list[str] = [
        "(created_source_at IS NULL OR created_source_at <= :cutoff)"
    ]
    params: dict[str, Any] = {"cutoff": scope.cutoff}
    if scope.time_from is not None:
        clauses.append("created_source_at >= :tf")
        params["tf"] = scope.time_from
    if scope.time_to is not None:
        clauses.append("created_source_at <= :tt")
        params["tt"] = scope.time_to
    if scope.all_user or scope.time_from is not None or scope.time_to is not None:
        rows = db.execute(sa_text(
            "SELECT id FROM retrieval_index_entries WHERE " + " AND ".join(clauses)
        ), params).all()
        ids |= {str(r[0]) for r in rows if r[0]}
    return ids


# ═══════════════════════════════════════════════════════════════════
# Scrub
# ═══════════════════════════════════════════════════════════════════

def scrub_raw_history(db: Session, scope: ContentScope) -> dict[str, int]:
    counts: dict[str, int] = {}
    if scope.preserve_raw_history:
        counts["turn_event_inbox"] = _execute_ids(
            db, "UPDATE turn_event_inbox SET payload = '{}' WHERE id IN :ids", scope.turn_event_inbox_ids)
        return counts
    counts["events"] = _execute_ids(
        db, "UPDATE events SET payload = '{}' WHERE id IN :ids", scope.raw_event_ids,
    )
    counts["turn_records"] = _execute_ids(
        db,
        "UPDATE turn_records SET response_payload = NULL "
        "WHERE id IN :ids OR turn_id IN :ids",
        scope.raw_turn_tokens,
    )
    counts["turn_event_inbox"] = _execute_ids(
        db, "UPDATE turn_event_inbox SET payload = '{}' WHERE id IN :ids",
        inbox_ids(db, scope),
    )
    return counts


def scrub_memory_derivations(db: Session, scope: ContentScope) -> dict[str, int]:
    counts: dict[str, int] = {}
    proposals = affected_proposal_ids(db, scope)
    if proposals:
        counts["memory_proposals"] = _execute_ids(
            db,
            "UPDATE memory_proposals SET content = NULL, structured_value = NULL, "
            "evidence = '[]', raw_payload = NULL, normalized_payload = NULL "
            "WHERE id IN :ids",
            proposals,
        )
    if scope.event_ids:
        counts["memory_evidence_span"] = _execute_ids(
            db,
            "UPDATE memory_evidence SET content_span = '' "
            "WHERE source_event_id IN :ids AND content_span IS NOT NULL",
            scope.event_ids,
        )
    if scope.memory_ids:
        counts["maintenance_inputs"] = _execute_ids(
            db,
            "UPDATE memory_maintenance_inputs SET snapshot_json = NULL "
            "WHERE memory_record_id IN :ids",
            scope.memory_ids,
        )
        action_ids: set[str] = set()
        action_ids |= _ids_from(
            db, "SELECT id FROM memory_maintenance_actions WHERE subject_memory_record_id IN :ids",
            scope.memory_ids,
        )
        action_ids |= set(select_json_array_members(
            db, "memory_maintenance_actions", "id", "related_record_ids",
            sorted(scope.memory_ids),
        ))
        if action_ids:
            counts["maintenance_actions"] = _execute_ids(
                db,
                "UPDATE memory_maintenance_actions SET details = NULL, preconditions = NULL "
                "WHERE id IN :ids",
                action_ids,
            )
        block_ids = set(select_json_array_members(
            db, "core_memory_blocks", "id", "source_memory_ids", sorted(scope.memory_ids),
        ))
        if block_ids:
            counts["core_memory_blocks"] = _execute_ids(
                db, "DELETE FROM core_memory_blocks WHERE id IN :ids", block_ids,
            )
    entries = retrieval_entry_ids(db, scope)
    if entries:
        counts["retrieval_index_tokens"] = _execute_ids(
            db, "DELETE FROM retrieval_index_tokens WHERE entry_id IN :ids", entries,
        )
        counts["retrieval_index_entries"] = _execute_ids(
            db,
            "UPDATE retrieval_index_entries SET title = '[forgotten]', search_text = '', "
            "snippet = '', metadata = '{}', is_searchable = false, is_current = false "
            "WHERE id IN :ids",
            entries,
        )
    return counts


def scrub_summaries_and_checkpoints(db: Session, scope: ContentScope) -> dict[str, int]:
    counts: dict[str, int] = {}
    summaries = affected_summary_ids(db, scope)
    if summaries:
        counts["segment_summaries"] = _execute_ids(
            db,
            "UPDATE segment_summaries SET goal = '[redacted]', outcome = '[redacted]', "
            "decisions = '[]', open_loops = '[]', entities = '[]', artifacts = '[]', "
            "important_tool_results = '[]', active_constraints = '[]', "
            "unresolved_failures = '[]', omitted_artifact_refs = '[]' WHERE id IN :ids",
            summaries,
        )
    checkpoints = affected_checkpoint_ids(db, scope)
    if checkpoints:
        counts["epoch_checkpoints"] = _execute_ids(
            db,
            "UPDATE epoch_checkpoints SET current_goal = '[redacted]', "
            "completed_milestones = '[]', open_loops = '[]', active_constraints = '[]', "
            "current_decisions = '[]', referenced_artifacts = '[]', "
            "relevant_entities = '[]', latest_verified_tool_states = '[]' WHERE id IN :ids",
            checkpoints,
        )
    compactions = compaction_input_ids(db, scope)
    if compactions:
        counts["compaction_inputs"] = _execute_ids(
            db,
            "UPDATE compaction_inputs SET working_state_snapshot = '{}', "
            "turn_manifest = '[]', event_manifest = '[]' WHERE id IN :ids",
            compactions,
        )
    epoch_compactions = epoch_compaction_input_ids(db, scope)
    if epoch_compactions:
        counts["epoch_compaction_inputs"] = _execute_ids(
            db,
            "UPDATE epoch_compaction_inputs SET current_objective = NULL, "
            "open_loops = '[]', active_constraints = '[]', artifact_refs = '[]', "
            "verified_tool_states = '[]' WHERE id IN :ids",
            epoch_compactions,
        )
    return counts


def scrub_runtime_tables(db: Session, scope: ContentScope) -> dict[str, int]:
    counts: dict[str, int] = {}
    working = working_state_ids(db, scope)
    if working:
        counts["working_states"] = _execute_ids(
            db,
            "UPDATE working_states SET current_objective = NULL, open_loops = '[]', "
            "active_constraints = '[]', pending_approvals = '[]', artifact_refs = '[]', "
            "verified_tool_states = '[]', uncommitted_side_effects = '[]', "
            "running_tool_state = '[]', applied_idempotency_keys = '[]' WHERE id IN :ids",
            working,
        )
    contexts = context_snapshot_ids(db, scope)
    if contexts:
        counts["context_snapshots"] = _execute_ids(
            db,
            "UPDATE context_snapshots SET context_items = '[]', meta = '{}' WHERE id IN :ids",
            contexts,
        )
    calls = llm_call_ids(db, scope)
    if calls:
        counts["llm_calls"] = _execute_ids(
            db,
            "UPDATE llm_calls SET input_preview = '', output_preview = '' WHERE id IN :ids",
            calls,
        )
    notes = task_note_ids(db, scope)
    if notes:
        counts["tasks"] = _execute_ids(
            db,
            "UPDATE tasks SET title = '[redacted]', description = '', condition = '' "
            "WHERE id IN :ids",
            notes,
        )
    artifacts = artifact_ids(db, scope)
    if artifacts:
        counts["artifacts"] = _execute_ids(
            db,
            "UPDATE artifacts SET content = '[redacted]' WHERE id IN :ids",
            artifacts,
        )
    attention = attention_ids(db, scope)
    if attention:
        counts["attention_states"] = _execute_ids(
            db,
            "UPDATE attention_states SET focus_topic = NULL, recent_topics = '[]' WHERE id IN :ids",
            attention,
        )
    tool_ops = tool_operation_ids(db, scope)
    if tool_ops:
        counts["tool_operations"] = _execute_ids(
            db,
            "UPDATE tool_operations SET params = '{}', result_payload = NULL, "
            "effect_receipt = '{}', error_message = NULL WHERE id IN :ids",
            tool_ops,
        )
    approvals = approval_request_ids(db, scope)
    if approvals:
        counts["approval_requests"] = _execute_ids(
            db,
            "UPDATE approval_requests SET tool_args = '{}', preconditions = '[]', "
            "effects = '[]', execution_result = NULL, error_message = NULL, "
            "risk_snapshot = '{}' WHERE id IN :ids",
            approvals,
        )
    title_ids = set(scope.thread_ids)
    if scope.all_user or scope.time_from is not None or scope.time_to is not None:
        tq = db.query(Thread.id).filter(Thread.created_at <= scope.cutoff)
        if scope.time_from is not None:
            tq = tq.filter(Thread.created_at >= scope.time_from)
        if scope.time_to is not None:
            tq = tq.filter(Thread.created_at <= scope.time_to)
        title_ids |= {str(r[0]) for r in tq.all() if r[0]}
    if title_ids:
        counts["threads"] = _execute_ids(
            db, "UPDATE threads SET title = '[redacted]' WHERE id IN :ids", title_ids,
        )
    return counts


def _replace_cleaned_refs(value: Any, removed: set[str]) -> Any:
    """递归把已清理的 action/evidence/artifact id 替换为 sentinel，保留结构。"""
    if isinstance(value, str):
        return "[forgotten]" if value in removed else value
    if isinstance(value, list):
        return [_replace_cleaned_refs(item, removed) for item in value]
    if isinstance(value, dict):
        return {key: _replace_cleaned_refs(item, removed) for key, item in value.items()}
    return value


def _json_has_ref(value: Any, ids: set[str]) -> bool:
    if isinstance(value, str):
        return value in ids
    if isinstance(value, list):
        return any(_json_has_ref(item, ids) for item in value)
    if isinstance(value, dict):
        return any(_json_has_ref(item, ids) for item in value.values())
    return False


def _scoped_array_rows(
    db: Session, table: str, id_col: str, array_col: str, ids: set[str],
) -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    for chunk in _chunks(ids):
        stmt = sa_text(
            f"SELECT {id_col}, {array_col} FROM {table} WHERE {id_col} IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        rows.extend(
            (str(r[0]), r[1]) for r in db.execute(stmt, {"ids": chunk}).all()
        )
    return rows


def _array_shape_unknown(value: Any) -> bool:
    """引用数组只接受字符串元素；对象/嵌套数组/数字等一律 fail-closed。"""
    if value is None:
        return False
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return True
    if not isinstance(value, list):
        return True
    return any(not isinstance(item, str) for item in value)


def _array_text_candidate_ids(
    db: Session, table: str, column: str, ids: set[str],
) -> set[str]:
    """引用 ID 在 JSON 文本中出现即视为候选（可发现嵌套对象/异常元素）。

    只匹配结构化 ID，不做内容关键词匹配；用于异常结构发现，不用于内容清理。
    """
    if not ids:
        return set()
    result: set[str] = set()
    for chunk in _chunks(ids):
        clauses = [f"CAST({column} AS TEXT) LIKE :p{i}" for i in range(len(chunk))]
        params = {f"p{i}": f"%{item}%" for i, item in enumerate(chunk)}
        rows = db.execute(sa_text(
            f"SELECT id FROM {table} WHERE " + " OR ".join(clauses)
        ), params).all()
        result |= {str(row[0]) for row in rows if row[0]}
    return result


def _all_user_candidate_ids(db: Session, table: str, cutoff: datetime) -> set[str]:
    rows = db.execute(sa_text(
        f"SELECT id FROM {table} WHERE created_at <= :cutoff"
    ), {"cutoff": cutoff}).all()
    return {str(row[0]) for row in rows if row[0]}


def unknown_reference_details(
    db: Session, scope: ContentScope,
) -> list[tuple[str, str, Any]]:
    """先按本次范围确定候选记录，再检查引用数组结构；异常元素 fail-closed。

    普通字符串成员查询命不中嵌套对象，故额外用「ID 在 JSON 文本中出现」发现
    候选；无关 Thread 且不含目标 ID 的异常数组不会被纳入。
    """
    checks: list[tuple[str, str, str, set[str]]] = []

    proposal_ids = set(affected_proposal_ids(db, scope))
    proposal_ids |= _array_text_candidate_ids(
        db, "memory_proposals", "source_event_ids", set(scope.event_ids),
    )
    if scope.memory_ids:
        proposal_ids |= _ids_from(
            db,
            "SELECT id FROM memory_proposals WHERE final_memory_id IN :ids",
            scope.memory_ids,
        )
    if scope.turn_tokens:
        proposal_ids |= _ids_from(
            db,
            "SELECT id FROM memory_proposals WHERE source_turn_id IN :ids "
            "OR source_turn_record_id IN :ids",
            scope.turn_tokens,
        )
    if scope.all_user:
        proposal_ids |= _all_user_candidate_ids(
            db, "memory_proposals", scope.cutoff,
        )
    if proposal_ids:
        checks.append(("memory_proposals", "id", "source_event_ids", proposal_ids))

    summary_ids = set(affected_summary_ids(db, scope))
    summary_ids |= _array_text_candidate_ids(
        db, "segment_summaries", "source_event_ids", set(scope.event_ids),
    )
    summary_ids |= _array_text_candidate_ids(
        db, "segment_summaries", "source_turn_ids", set(scope.turn_tokens),
    )
    if scope.all_user:
        summary_ids |= _all_user_candidate_ids(
            db, "segment_summaries", scope.cutoff,
        )
    if summary_ids:
        checks.append(("segment_summaries", "id", "source_event_ids", summary_ids))
        checks.append(("segment_summaries", "id", "source_turn_ids", summary_ids))

    checkpoint_ids = set(affected_checkpoint_ids(db, scope))
    checkpoint_ids |= _array_text_candidate_ids(
        db, "epoch_checkpoints", "source_segment_ids", set(scope.segment_ids),
    )
    if scope.all_user:
        checkpoint_ids |= _all_user_candidate_ids(
            db, "epoch_checkpoints", scope.cutoff,
        )
    if checkpoint_ids:
        checks.append(("epoch_checkpoints", "id", "source_segment_ids", checkpoint_ids))

    if scope.memory_ids:
        action_ids = set(select_json_array_members(
            db, "memory_maintenance_actions", "id", "related_record_ids",
            sorted(scope.memory_ids),
        ))
        action_ids |= _array_text_candidate_ids(
            db, "memory_maintenance_actions", "related_record_ids",
            set(scope.memory_ids),
        )
        action_ids |= _ids_from(
            db,
            "SELECT id FROM memory_maintenance_actions "
            "WHERE subject_memory_record_id IN :ids",
            scope.memory_ids,
        )
        if scope.all_user:
            action_ids |= _all_user_candidate_ids(
                db, "memory_maintenance_actions", scope.cutoff,
            )
        if action_ids:
            checks.append((
                "memory_maintenance_actions", "id", "related_record_ids", action_ids,
            ))
        block_ids = set(select_json_array_members(
            db, "core_memory_blocks", "id", "source_memory_ids",
            sorted(scope.memory_ids),
        ))
        block_ids |= _array_text_candidate_ids(
            db, "core_memory_blocks", "source_memory_ids", set(scope.memory_ids),
        )
        if scope.all_user:
            block_ids |= _all_user_candidate_ids(
                db, "core_memory_blocks", scope.cutoff,
            )
        if block_ids:
            checks.append((
                "core_memory_blocks", "id", "source_memory_ids", block_ids,
            ))

    details: list[tuple[str, str, Any]] = []
    for table, id_col, array_col, ids in checks:
        for _row_id, value in _scoped_array_rows(db, table, id_col, array_col, ids):
            if _array_shape_unknown(value):
                details.append((table, array_col, value))
    return details


def count_unknown_references(db: Session, scope: ContentScope) -> int:
    return len(unknown_reference_details(db, scope))


def _redact_task_state_refs(
    db: Session, task_ids: set[str], removed: set[str],
) -> int:
    """清理父 Task 的 task_state / task_brief 中对已清理子记录的引用。

    若运行中的任务引用了被清理的记录，恢复后无法安全继续执行 → 转合法终态
    cancelled；终端任务保持原状态。
    """
    active = {
        "queued", "dispatching", "running", "blocked_approval", "blocked_user",
        "blocked_node", "reconciling", "verifying",
    }
    changed_count = 0
    for task in db.query(AgentTask).filter(AgentTask.id.in_(task_ids)).all():
        changed = False
        for attr in ("task_state", "task_brief"):
            current = getattr(task, attr)
            updated = _replace_cleaned_refs(current, removed)
            if updated != current:
                setattr(task, attr, updated)
                changed = True
        if changed:
            changed_count += 1
            if (task.status or "") in active:
                task.status = "cancelled"
                task.completed_at = datetime.now(timezone.utc)
    return changed_count


# 每个实际 Scrub 的字段必须有对应 Verify 残留检查。新增清理字段时必须同时加入
# 两个集合；tests/integration/backend/test_forget_content_coverage.py 会断言
# SCRUBBED_FIELDS 是 VERIFIED_FIELDS 的子集。
SCRUBBED_FIELDS: frozenset[str] = frozenset({
    "events.payload", "turn_records.response_payload", "turn_event_inbox.payload",
    "memory_records.content", "memory_records.structured_value", "memory_records.graph_facts",
    "memory_proposals.content", "memory_proposals.structured_value", "memory_proposals.evidence",
    "memory_proposals.raw_payload", "memory_proposals.normalized_payload",
    "memory_evidence.content_span",
    "memory_maintenance_inputs.snapshot_json",
    "memory_maintenance_actions.details", "memory_maintenance_actions.preconditions",
    "core_memory_blocks.content",
    "retrieval_index_entries.title", "retrieval_index_entries.search_text",
    "retrieval_index_entries.snippet", "retrieval_index_entries.metadata",
    "retrieval_index_entries.is_searchable", "retrieval_index_entries.is_current",
    "retrieval_index_tokens.entry_id",
    "segment_summaries.goal", "segment_summaries.outcome", "segment_summaries.decisions",
    "segment_summaries.open_loops", "segment_summaries.entities", "segment_summaries.artifacts",
    "segment_summaries.important_tool_results", "segment_summaries.active_constraints",
    "segment_summaries.unresolved_failures", "segment_summaries.omitted_artifact_refs",
    "epoch_checkpoints.current_goal", "epoch_checkpoints.completed_milestones",
    "epoch_checkpoints.open_loops", "epoch_checkpoints.active_constraints",
    "epoch_checkpoints.current_decisions", "epoch_checkpoints.referenced_artifacts",
    "epoch_checkpoints.relevant_entities", "epoch_checkpoints.latest_verified_tool_states",
    "compaction_inputs.working_state_snapshot", "compaction_inputs.turn_manifest",
    "compaction_inputs.event_manifest",
    "epoch_compaction_inputs.current_objective", "epoch_compaction_inputs.open_loops",
    "epoch_compaction_inputs.active_constraints", "epoch_compaction_inputs.artifact_refs",
    "epoch_compaction_inputs.verified_tool_states",
    "working_states.current_objective", "working_states.open_loops",
    "working_states.active_constraints", "working_states.pending_approvals",
    "working_states.artifact_refs", "working_states.verified_tool_states",
    "working_states.uncommitted_side_effects", "working_states.running_tool_state",
    "working_states.applied_idempotency_keys",
    "context_snapshots.context_items", "context_snapshots.meta",
    "llm_calls.input_preview", "llm_calls.output_preview",
    "tasks.title", "tasks.description", "tasks.condition",
    "artifacts.content",
    "attention_states.focus_topic", "attention_states.recent_topics",
    "threads.title",
    "agent_task_watches.config", "agent_task_watches.cursor",
    "desktop_action_receipts.result_payload", "desktop_action_receipts.error_message",
    "tool_operations.params", "tool_operations.result_payload",
    "tool_operations.effect_receipt", "tool_operations.error_message",
    "approval_requests.tool_args", "approval_requests.preconditions",
    "approval_requests.effects", "approval_requests.execution_result",
    "approval_requests.risk_snapshot", "approval_requests.error_message",
    "agent_tasks.title", "agent_tasks.goal", "agent_tasks.task_brief",
    "agent_tasks.task_state", "agent_tasks.report", "agent_tasks.last_error",
    "agent_runs.input_snapshot", "agent_runs.output_summary", "agent_runs.error_message",
    "agent_actions.arguments", "agent_actions.preconditions", "agent_actions.effects",
    "agent_actions.result_summary", "agent_actions.evidence_refs",
    "agent_actions.error_message",
    "agent_task_events.payload",
    "agent_task_checkpoints.state", "agent_task_checkpoints.plan",
    "agent_task_checkpoints.pending_action_refs", "agent_task_checkpoints.evidence_refs",
    "task_evidence.summary", "task_evidence.inline_payload",
    "task_evidence.object_bucket", "task_evidence.object_key",
    "task_artifacts.name", "task_artifacts.summary",
    "task_artifacts.object_bucket", "task_artifacts.object_key",
    "documents.status", "documents.title", "documents.object_bucket", "documents.object_key",
    "chunks.content", "knowledge_chunk_vectors.chunk_id",
    "memory_conflict_cases.old_content", "memory_conflict_cases.new_content",
    "memory_conflict_cases.proposal_payload", "memory_conflict_cases.evidence",
    "memory_conflict_cases.graph_evidence", "memory_conflict_cases.question",
    "memory_conflict_cases.verdict_reason", "memory_conflict_cases.decision_span",
    "memory_conflict_cases.resolution_reason",
    "memory_recall_runs.request_query", "memory_recall_runs.scope_context",
    "memory_recall_runs.routes_executed",
    "retrieval_runs.query",
    "outbox_jobs.payload", "outbox_jobs.error_message",
})

VERIFIED_FIELDS: frozenset[str] = frozenset({
    "events.payload", "turn_records.response_payload", "turn_event_inbox.payload",
    "memory_records.content", "memory_records.structured_value", "memory_records.graph_facts",
    "memory_proposals.content", "memory_proposals.structured_value", "memory_proposals.evidence",
    "memory_proposals.raw_payload", "memory_proposals.normalized_payload",
    "memory_evidence.content_span",
    "memory_maintenance_inputs.snapshot_json",
    "memory_maintenance_actions.details", "memory_maintenance_actions.preconditions",
    "core_memory_blocks.content",
    "retrieval_index_entries.title", "retrieval_index_entries.search_text",
    "retrieval_index_entries.snippet", "retrieval_index_entries.metadata",
    "retrieval_index_entries.is_searchable", "retrieval_index_entries.is_current",
    "retrieval_index_tokens.entry_id",
    "segment_summaries.goal", "segment_summaries.outcome", "segment_summaries.decisions",
    "segment_summaries.open_loops", "segment_summaries.entities", "segment_summaries.artifacts",
    "segment_summaries.important_tool_results", "segment_summaries.active_constraints",
    "segment_summaries.unresolved_failures", "segment_summaries.omitted_artifact_refs",
    "epoch_checkpoints.current_goal", "epoch_checkpoints.completed_milestones",
    "epoch_checkpoints.open_loops", "epoch_checkpoints.active_constraints",
    "epoch_checkpoints.current_decisions", "epoch_checkpoints.referenced_artifacts",
    "epoch_checkpoints.relevant_entities", "epoch_checkpoints.latest_verified_tool_states",
    "compaction_inputs.working_state_snapshot", "compaction_inputs.turn_manifest",
    "compaction_inputs.event_manifest",
    "epoch_compaction_inputs.current_objective", "epoch_compaction_inputs.open_loops",
    "epoch_compaction_inputs.active_constraints", "epoch_compaction_inputs.artifact_refs",
    "epoch_compaction_inputs.verified_tool_states",
    "working_states.current_objective", "working_states.open_loops",
    "working_states.active_constraints", "working_states.pending_approvals",
    "working_states.artifact_refs", "working_states.verified_tool_states",
    "working_states.uncommitted_side_effects", "working_states.running_tool_state",
    "working_states.applied_idempotency_keys",
    "context_snapshots.context_items", "context_snapshots.meta",
    "llm_calls.input_preview", "llm_calls.output_preview",
    "tasks.title", "tasks.description", "tasks.condition",
    "artifacts.content",
    "attention_states.focus_topic", "attention_states.recent_topics",
    "threads.title",
    "agent_task_watches.config", "agent_task_watches.cursor",
    "desktop_action_receipts.result_payload", "desktop_action_receipts.error_message",
    "tool_operations.params", "tool_operations.result_payload",
    "tool_operations.effect_receipt", "tool_operations.error_message",
    "approval_requests.tool_args", "approval_requests.preconditions",
    "approval_requests.effects", "approval_requests.execution_result",
    "approval_requests.risk_snapshot", "approval_requests.error_message",
    "agent_tasks.title", "agent_tasks.goal", "agent_tasks.task_brief",
    "agent_tasks.task_state", "agent_tasks.report", "agent_tasks.last_error",
    "agent_runs.input_snapshot", "agent_runs.output_summary", "agent_runs.error_message",
    "agent_actions.arguments", "agent_actions.preconditions", "agent_actions.effects",
    "agent_actions.result_summary", "agent_actions.evidence_refs",
    "agent_actions.error_message",
    "agent_task_events.payload",
    "agent_task_checkpoints.state", "agent_task_checkpoints.plan",
    "agent_task_checkpoints.pending_action_refs", "agent_task_checkpoints.evidence_refs",
    "task_evidence.summary", "task_evidence.inline_payload",
    "task_evidence.object_bucket", "task_evidence.object_key",
    "task_artifacts.name", "task_artifacts.summary",
    "task_artifacts.object_bucket", "task_artifacts.object_key",
    "documents.status", "documents.title", "documents.object_bucket", "documents.object_key",
    "chunks.content", "knowledge_chunk_vectors.chunk_id",
    "memory_conflict_cases.old_content", "memory_conflict_cases.new_content",
    "memory_conflict_cases.proposal_payload", "memory_conflict_cases.evidence",
    "memory_conflict_cases.graph_evidence", "memory_conflict_cases.question",
    "memory_conflict_cases.verdict_reason", "memory_conflict_cases.decision_span",
    "memory_conflict_cases.resolution_reason",
    "memory_recall_runs.request_query", "memory_recall_runs.scope_context",
    "memory_recall_runs.routes_executed",
    "retrieval_runs.query",
    "outbox_jobs.payload", "outbox_jobs.error_message",
})

def scrub_auxiliary_tables(db: Session, scope: ContentScope) -> dict[str, int]:
    counts: dict[str, int] = {}
    cases = scope.conflict_case_ids
    if cases:
        counts["memory_conflict_cases"] = _execute_ids(
            db,
            "UPDATE memory_conflict_cases SET old_content = '[redacted]', "
            "new_content = '[redacted]', proposal_payload = '{}', evidence = '[]', "
            "graph_evidence = '[]', question = '[redacted]', verdict_reason = '', "
            "decision_span = '', resolution_reason = '', "
            "status = CASE WHEN status IN ('pending','asking') THEN 'stale' ELSE status END "
            "WHERE id IN :ids",
            cases,
        )
    recall = scope.recall_run_ids
    if recall:
        counts["memory_recall_runs"] = _execute_ids(
            db,
            "UPDATE memory_recall_runs SET request_query = '[redacted]', "
            "scope_context = '{}', routes_executed = '[]' WHERE id IN :ids",
            recall,
        )
    retrieval = scope.retrieval_run_ids
    if retrieval:
        counts["retrieval_runs"] = _execute_ids(
            db, "UPDATE retrieval_runs SET query = '[redacted]' WHERE id IN :ids",
            retrieval,
        )
    if scope.outbox_job_ids:
        changed = 0
        for job in db.query(OutboxJob).filter(OutboxJob.id.in_(scope.outbox_job_ids)).all():
            if not is_internal_job(job.job_type):
                payload = job.payload or {}
                redacted = redact_job_payload(job.job_type, payload)
                if redacted != payload:
                    job.payload = redacted
                    changed += 1
            if job.error_message:
                job.error_message = ""
                changed += 1
        counts["outbox_jobs"] = changed
    return counts


def scrub_task_runtime(
    db: Session, scope: ContentScope,
) -> tuple[dict[str, int], set[tuple[str, str]]]:
    """按独立范围清理 Task Runtime 父任务与子记录，返回计数与对象引用。

    父 Task 命中只清理父正文/语义状态；每条子记录按自身时间独立判断，绝不
    因为父 Task 命中就清理其全部历史子记录。
    """
    counts: dict[str, int] = {}
    refs: set[tuple[str, str]] = set()
    task_ids = scope.agent_task_ids
    partial_ids = scope.partial_agent_task_ids
    run_ids = scope.agent_run_ids
    action_ids = scope.agent_action_ids
    event_ids = scope.agent_task_event_ids
    checkpoint_ids = scope.agent_task_checkpoint_ids
    evidence_ids = scope.task_evidence_ids
    artifact_ids = scope.task_artifact_ids
    cleaned_ids = (
        set(run_ids) | set(action_ids) | set(event_ids) | set(checkpoint_ids)
        | set(evidence_ids) | set(artifact_ids)
    )
    if not task_ids and not cleaned_ids:
        return counts, refs

    # 1) 命中的父 Task：清理正文与语义状态；运行态转 cancelled。
    if task_ids:
        active_states = (
            "'queued','dispatching','running','blocked_approval','blocked_user',"
            "'blocked_node','reconciling','verifying'"
        )
        counts["agent_tasks"] = _execute_ids(
            db,
            "UPDATE agent_tasks SET title = '[redacted]', goal = '[redacted]', "
            "task_brief = '{}', task_state = '{}', report = NULL, last_error = NULL, "
            f"status = CASE WHEN status IN ({active_states}) THEN 'cancelled' ELSE status END, "
            f"completed_at = CASE WHEN status IN ({active_states}) THEN :now ELSE completed_at END "
            "WHERE id IN :ids",
            task_ids,
            {"now": datetime.now(timezone.utc)},
        )

    # 2) 命中的子记录：逐条按 id 清理，不按 task_id 扩大。
    if run_ids:
        counts["agent_runs"] = _execute_ids(
            db,
            "UPDATE agent_runs SET input_snapshot = '{}', output_summary = NULL, "
            "error_message = NULL WHERE id IN :ids",
            run_ids,
        )
    if action_ids:
        counts["agent_actions"] = _execute_ids(
            db,
            "UPDATE agent_actions SET arguments = '{}', preconditions = '[]', "
            "effects = '[]', result_summary = NULL, evidence_refs = '[]', "
            "error_message = NULL WHERE id IN :ids",
            action_ids,
        )
    if event_ids:
        counts["agent_task_events"] = _execute_ids(
            db, "UPDATE agent_task_events SET payload = '{}' WHERE id IN :ids",
            event_ids,
        )
    if checkpoint_ids:
        counts["agent_task_checkpoints"] = _execute_ids(
            db,
            "UPDATE agent_task_checkpoints SET state = '{}', plan = '{}', "
            "pending_action_refs = '[]', evidence_refs = '[]' WHERE id IN :ids",
            checkpoint_ids,
        )
    if evidence_ids:
        for bucket, key in db.query(
            TaskEvidence.object_bucket, TaskEvidence.object_key,
        ).filter(
            TaskEvidence.id.in_(evidence_ids),
            TaskEvidence.object_bucket.isnot(None),
            TaskEvidence.object_key.isnot(None),
        ).all():
            if bucket and key:
                refs.add((str(bucket), str(key)))
        counts["task_evidence"] = _execute_ids(
            db,
            "UPDATE task_evidence SET summary = '[redacted]', inline_payload = NULL, "
            "object_bucket = NULL, object_key = NULL WHERE id IN :ids",
            evidence_ids,
        )
    if artifact_ids:
        for bucket, key in db.query(
            TaskArtifact.object_bucket, TaskArtifact.object_key,
        ).filter(
            TaskArtifact.id.in_(artifact_ids),
            TaskArtifact.object_key != "",
        ).all():
            if bucket and key:
                refs.add((str(bucket), str(key)))
        counts["task_artifacts"] = _execute_ids(
            db,
            "UPDATE task_artifacts SET name = '[redacted]', summary = '[redacted]', "
            "object_bucket = '', object_key = '' WHERE id IN :ids",
            artifact_ids,
        )

    # 3) Task 附属运行数据（watch / desktop receipt）
    watch_task_ids = task_ids | partial_ids
    if watch_task_ids:
        watch_ids = _ids_from(
            db, "SELECT id FROM agent_task_watches WHERE task_id IN :ids",
            watch_task_ids,
        )
        if watch_ids:
            counts["agent_task_watches"] = _execute_ids(
                db,
                "UPDATE agent_task_watches SET config = '{}', cursor = '{}' "
                "WHERE id IN :ids",
                watch_ids,
            )
    receipt_action_ids = set(action_ids)
    if receipt_action_ids:
        counts["desktop_action_receipts"] = _execute_ids(
            db,
            "UPDATE desktop_action_receipts SET result_payload = NULL, "
            "error_message = NULL WHERE action_id IN :ids",
            receipt_action_ids,
        )

    # 4) 父 Task 未自身命中但子记录命中：只脱敏 task_state/task_brief 中对
    #    已清理子记录的引用；引用确实变化时运行态任务才转 cancelled。
    if cleaned_ids and partial_ids:
        counts["agent_task_state_refs"] = _redact_task_state_refs(
            db, partial_ids, cleaned_ids,
        )
    return counts, refs


def _record_knowledge_chunk_ids(
    db: Session, operation_id: str, chunk_ids: list[str],
) -> None:
    ikey = f"fo:{operation_id}:knowledge_chunks"
    action = db.query(ForgetAction).filter_by(idempotency_key=ikey).first()
    if action is None:
        db.add(ForgetAction(
            id=str(uuid.uuid4()),
            forget_operation_id=operation_id,
            action_type="scrub_knowledge_chunks",
            batch_no=0,
            status="done",
            idempotency_key=ikey,
            details={"chunk_ids": sorted(chunk_ids)},
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        ))
        db.flush()
        return
    action.details = {"chunk_ids": sorted(chunk_ids)}
    db.flush()


def scrub_knowledge(
    db: Session, scope: ContentScope,
) -> tuple[dict[str, int], set[tuple[str, str]]]:
    counts: dict[str, int] = {}
    refs: set[tuple[str, str]] = set()
    docs = scope.document_ids
    if not docs:
        return counts, refs
    chunk_ids = document_chunk_ids(db, docs)
    if chunk_ids:
        # 分块删除后无法再由 document_id 反查向量；持久化本次目标 chunk_ids，
        # 供 Verify 重新统计向量残留。
        _record_knowledge_chunk_ids(db, scope.operation_id, chunk_ids)
        counts["knowledge_chunk_vectors"] = _execute_ids(
            db, "DELETE FROM knowledge_chunk_vectors WHERE chunk_id IN :ids", chunk_ids,
        )
        counts["chunks"] = _execute_ids(
            db, "DELETE FROM chunks WHERE id IN :ids", chunk_ids,
        )
    for bucket, key in db.query(Document.object_bucket, Document.object_key).filter(
        Document.id.in_(docs),
        Document.object_bucket.isnot(None),
        Document.object_key.isnot(None),
    ).all():
        if bucket and key:
            refs.add((str(bucket), str(key)))
    counts["documents"] = _execute_ids(
        db,
        "UPDATE documents SET status = 'deleted', deleted_at = :now, superseded_by = NULL, "
        "title = '[redacted]', object_bucket = NULL, object_key = NULL WHERE id IN :ids",
        docs,
        {"now": datetime.now(timezone.utc)},
    )
    return counts, refs


# ═══════════════════════════════════════════════════════════════════
# 对象存储：登记 / 删除 / 验证
# ═══════════════════════════════════════════════════════════════════

def _object_action_key(operation_id: str, bucket: str, key: str) -> str:
    digest = hashlib.sha256(f"{bucket}\0{key}".encode("utf-8")).hexdigest()
    return f"fo:{operation_id}:do:{digest}"


def queue_object_deletions(
    db: Session, operation_id: str, refs: set[tuple[str, str]],
) -> int:
    """登记待删除对象（幂等）。返回新登记数量。"""
    queued = 0
    for bucket, key in sorted(refs):
        ikey = _object_action_key(operation_id, bucket, key)
        existing = db.query(ForgetAction.id).filter_by(idempotency_key=ikey).first()
        if existing is not None:
            continue
        db.add(ForgetAction(
            id=str(uuid.uuid4()),
            forget_operation_id=operation_id,
            action_type="delete_object",
            batch_no=0,
            status="pending",
            idempotency_key=ikey,
            details={"bucket": bucket, "key": key},
        ))
        queued += 1
    db.flush()
    return queued


def process_object_deletions(db: Session, operation_id: str) -> dict[str, Any]:
    """提交后删除对象；失败保留 failed 动作供重试。"""
    done = 0
    failed: list[str] = []
    actions = db.query(ForgetAction).filter(
        ForgetAction.forget_operation_id == operation_id,
        ForgetAction.action_type == "delete_object",
        ForgetAction.status.in_(["pending", "failed"]),
    ).all()
    for action in actions:
        details = action.details or {}
        bucket = str(details.get("bucket") or "")
        key = str(details.get("key") or "")
        if not bucket or not key:
            action.status = "failed"
            action.error_message = "invalid object ref"
            failed.append("invalid_ref")
            db.commit()
            continue
        try:
            # 删除前再次确认没有非目标行引用（防止排队后被新记录复用）。
            if verify_object_shared_refs(db, bucket, key) > 0:
                action.status = "done"
                action.details = {**details, "skipped_shared": True}
                action.completed_at = datetime.now(timezone.utc)
                db.commit()
                continue
            ref = object_store.ObjectRef(bucket, key)
            if not object_store.exists(ref):
                action.status = "done"
            else:
                object_store.delete(ref)
                action.status = "done"
            action.completed_at = datetime.now(timezone.utc)
            action.error_message = None
            done += 1
        except Exception as exc:  # noqa: BLE001 - 记录失败供重试
            action.status = "failed"
            action.error_message = f"{type(exc).__name__}: {exc}"[:500]
            failed.append(f"{bucket}/{key}")
        db.commit()
    return {"deleted": done, "failed": failed}


def pending_object_actions(db: Session, operation_id: str) -> list[ForgetAction]:
    return db.query(ForgetAction).filter(
        ForgetAction.forget_operation_id == operation_id,
        ForgetAction.action_type == "delete_object",
    ).all()


# ═══════════════════════════════════════════════════════════════════
# Verify
# ═══════════════════════════════════════════════════════════════════

@dataclass
class VerifyReport:
    targets: dict[str, int] = field(default_factory=dict)
    scrubbed: dict[str, int] = field(default_factory=dict)
    residues: dict[str, int] = field(default_factory=dict)
    object_actions: dict[str, int] = field(default_factory=dict)
    unknown: int = 0
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "targets": self.targets,
            "scrubbed": self.scrubbed,
            "residues": self.residues,
            "object_actions": self.object_actions,
            "unknown": self.unknown,
            "failures": self.failures,
            "warnings": self.warnings,
        }

    def add(self, label: str, count: int) -> None:
        if count:
            self.residues[label] = count
            self.failures.append(f"{label}: {count} residue rows")

    def warn(self, label: str, count: int) -> None:
        '''显式记录但不判定失败：仅表明该维度无法证明（不静默丢弃）。'''
        if count:
            self.warnings.append(f"{label}: {count}")


def _count_unknown_json_shapes(
    db: Session, table: str, column: str,
) -> int:
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        sql = sa_text(
            f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL AND "
            f"jsonb_typeof(CAST({column} AS jsonb)) NOT IN ('array', 'null')"
        )
    elif dialect == "sqlite":
        sql = sa_text(
            f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL AND "
            f"json_type({column}) NOT IN ('array', 'null')"
        )
    else:  # pragma: no cover
        return 0
    return int(db.execute(sql).scalar() or 0)


def count_unknown_json_shapes(db: Session) -> int:
    """统计无法按数组解析的引用字段：无法证明即不得严格完成。"""
    return sum(
        _count_unknown_json_shapes(db, table, column)
        for table, column in (
            ("memory_proposals", "source_event_ids"),
            ("segment_summaries", "source_event_ids"),
            ("segment_summaries", "source_turn_ids"),
            ("epoch_checkpoints", "source_segment_ids"),
            ("core_memory_blocks", "source_memory_ids"),
            ("memory_maintenance_actions", "related_record_ids"),
        )
    )


def _sum_in(
    db: Session, sql: str, ids: Iterable[str], extra: dict[str, Any] | None = None,
) -> int:
    """IN :ids 分批求和，避免 SQLite/PG 参数上限。"""
    total = 0
    for chunk in _chunks(ids):
        stmt = sa_text(sql).bindparams(bindparam("ids", expanding=True))
        total += int(db.execute(stmt, {**(extra or {}), "ids": chunk}).scalar() or 0)
    return total


def _count_nonempty_text(
    db: Session, table: str, column: str, ids: set[str],
    empty_values: tuple[str, ...] = ("", "[redacted]", "[forgotten]"),
) -> int:
    if not ids:
        return 0
    total = 0
    placeholders = ", ".join(f":e{i}" for i in range(len(empty_values)))
    for chunk in _chunks(ids):
        stmt = sa_text(
            f"SELECT COUNT(*) FROM {table} WHERE id IN :ids AND "
            f"COALESCE(CAST({column} AS TEXT), '') NOT IN ({placeholders})"
        ).bindparams(bindparam("ids", expanding=True))
        params = {**{f"e{i}": v for i, v in enumerate(empty_values)}, "ids": chunk}
        total += int(db.execute(stmt, params).scalar() or 0)
    return total


def _count_nonempty_json(
    db: Session, table: str, column: str, ids: set[str],
    empty: tuple[str, ...] = ("[]", "{}", "null", '""', "", "[redacted]"),
) -> int:
    if not ids:
        return 0
    total = 0
    placeholders = ", ".join(f":e{i}" for i in range(len(empty)))
    for chunk in _chunks(ids):
        stmt = sa_text(
            f"SELECT COUNT(*) FROM {table} WHERE id IN :ids AND {column} IS NOT NULL AND "
            f"COALESCE(CAST({column} AS TEXT), '') NOT IN ({placeholders})"
        ).bindparams(bindparam("ids", expanding=True))
        params = {**{f"e{i}": v for i, v in enumerate(empty)}, "ids": chunk}
        total += int(db.execute(stmt, params).scalar() or 0)
    return total


def verify_scope(db: Session, scope: ContentScope) -> VerifyReport:
    report = VerifyReport()
    report.unknown = count_unknown_references(db, scope)
    if report.unknown:
        report.failures.append(
            f"unknown JSON reference shapes: {report.unknown}"
        )
    report.targets = {
        "memory_records": len(scope.memory_ids),
        "events": len(scope.event_ids),
        "turns": len(scope.turn_tokens),
        "segments": len(scope.segment_ids),
        "epochs": len(scope.epoch_ids),
        "agent_tasks": len(scope.agent_task_ids),
        "documents": len(scope.document_ids),
    }

    # Memory
    if scope.memory_ids:
        report.add("memory_records", _sum_in(
            db,
            "SELECT COUNT(*) FROM memory_records WHERE id IN :ids AND "
            "(content <> '[forgotten]' OR structured_value IS NOT NULL OR graph_facts IS NOT NULL)",
            scope.memory_ids,
        ))
        report.add("memory_evidence", _sum_in(
            db,
            "SELECT COUNT(*) FROM memory_evidence WHERE memory_id IN :ids",
            scope.memory_ids,
        ))

    proposals = affected_proposal_ids(db, scope)
    report.scrubbed["memory_proposals"] = len(proposals)
    if proposals:
        report.add("memory_proposals.content", _count_nonempty_text(
            db, "memory_proposals", "content", proposals))
        for column in ("structured_value", "evidence", "raw_payload", "normalized_payload"):
            report.add(f"memory_proposals.{column}", _count_nonempty_json(
                db, "memory_proposals", column, proposals))

    if scope.event_ids:
        report.add("memory_evidence.content_span", _sum_in(
            db,
            "SELECT COUNT(*) FROM memory_evidence WHERE source_event_id IN :ids "
            "AND COALESCE(content_span, '') <> ''",
            scope.event_ids,
        ))
    if scope.memory_ids:
        report.add("memory_maintenance_inputs", _sum_in(
            db,
            "SELECT COUNT(*) FROM memory_maintenance_inputs WHERE memory_record_id IN :ids "
            "AND snapshot_json IS NOT NULL",
            scope.memory_ids,
        ))
        block_residue = count_json_array_members(
            db, "core_memory_blocks", "source_memory_ids", sorted(scope.memory_ids),
        )
        report.add("core_memory_blocks", block_residue)
        block_ids = set(select_json_array_members(
            db, "core_memory_blocks", "id", "source_memory_ids",
            sorted(scope.memory_ids),
        ))
        if block_ids:
            report.add("core_memory_blocks.content", _count_nonempty_text(
                db, "core_memory_blocks", "content", block_ids))
        report.add("memory_maintenance_actions", _sum_in(
            db,
            "SELECT COUNT(*) FROM memory_maintenance_actions WHERE subject_memory_record_id IN :ids "
            "AND (details IS NOT NULL OR preconditions IS NOT NULL)",
            scope.memory_ids,
        ))
        related_actions = set(select_json_array_members(
            db, "memory_maintenance_actions", "id", "related_record_ids",
            sorted(scope.memory_ids),
        ))
        if related_actions:
            report.add("memory_maintenance_actions.related", int(db.execute(
                sa_text(
                    "SELECT COUNT(*) FROM memory_maintenance_actions WHERE id IN :ids "
                    "AND (details IS NOT NULL OR preconditions IS NOT NULL)"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": sorted(related_actions)},
            ).scalar() or 0))

    entries = retrieval_entry_ids(db, scope)
    if entries:
        for column in ("title", "search_text", "snippet"):
            report.add(f"retrieval.{column}", _count_nonempty_text(
                db, "retrieval_index_entries", column, entries))
        report.add("retrieval.metadata", _count_nonempty_json(
            db, "retrieval_index_entries", "metadata", entries))
        report.add("retrieval.searchable", _sum_in(
            db,
            "SELECT COUNT(*) FROM retrieval_index_entries WHERE id IN :ids AND is_searchable = true",
            entries,
        ))
        report.add("retrieval.current", _sum_in(
            db,
            "SELECT COUNT(*) FROM retrieval_index_entries WHERE id IN :ids AND is_current = true",
            entries,
        ))
        report.add("retrieval.tokens", _sum_in(
            db,
            "SELECT COUNT(*) FROM retrieval_index_tokens WHERE entry_id IN :ids",
            entries,
        ))

    # Raw history（memory_only 允许保留）
    if not scope.preserve_raw_history:
        report.add("events.payload", _sum_in(
            db,
            "SELECT COUNT(*) FROM events WHERE id IN :ids AND payload IS NOT NULL "
            "AND CAST(payload AS TEXT) NOT IN ('{}', 'null')",
            scope.raw_event_ids,
        ))
        report.add("turn_records.response_payload", _sum_in(
            db,
            "SELECT COUNT(*) FROM turn_records WHERE (id IN :ids OR turn_id IN :ids) "
            "AND response_payload IS NOT NULL",
            scope.raw_turn_tokens,
        ))
    inbox = scope.turn_event_inbox_ids if scope.preserve_raw_history else inbox_ids(db, scope)
    report.add("turn_event_inbox.payload", _sum_in(
        db,
        "SELECT COUNT(*) FROM turn_event_inbox WHERE id IN :ids AND payload IS NOT NULL "
        "AND CAST(payload AS TEXT) NOT IN ('{}', 'null')",
        inbox,
    ))

    # Summaries / checkpoints / compaction
    summaries = affected_summary_ids(db, scope)
    report.scrubbed["segment_summaries"] = len(summaries)
    if summaries:
        for column in ("goal", "outcome"):
            report.add(f"segment_summaries.{column}", _count_nonempty_text(
                db, "segment_summaries", column, summaries))
        for column in (
            "decisions", "open_loops", "entities", "artifacts",
            "important_tool_results", "active_constraints",
            "unresolved_failures", "omitted_artifact_refs",
        ):
            report.add(f"segment_summaries.{column}", _count_nonempty_json(
                db, "segment_summaries", column, summaries))

    checkpoints = affected_checkpoint_ids(db, scope)
    report.scrubbed["epoch_checkpoints"] = len(checkpoints)
    if checkpoints:
        report.add("epoch_checkpoints.current_goal", _count_nonempty_text(
            db, "epoch_checkpoints", "current_goal", checkpoints))
        for column in (
            "completed_milestones", "open_loops", "active_constraints",
            "current_decisions", "referenced_artifacts", "relevant_entities",
            "latest_verified_tool_states",
        ):
            report.add(f"epoch_checkpoints.{column}", _count_nonempty_json(
                db, "epoch_checkpoints", column, checkpoints))

    compactions = compaction_input_ids(db, scope)
    if compactions:
        report.add("compaction_inputs.working_state_snapshot", _count_nonempty_json(
            db, "compaction_inputs", "working_state_snapshot", compactions))
        report.add("compaction_inputs.event_manifest", _count_nonempty_json(
            db, "compaction_inputs", "event_manifest", compactions))
        report.add("compaction_inputs.turn_manifest", _count_nonempty_json(
            db, "compaction_inputs", "turn_manifest", compactions))
    epoch_compactions = epoch_compaction_input_ids(db, scope)
    if epoch_compactions:
        report.add("epoch_compaction_inputs.current_objective", _count_nonempty_text(
            db, "epoch_compaction_inputs", "current_objective", epoch_compactions))
        for column in (
            "open_loops", "active_constraints", "artifact_refs", "verified_tool_states",
        ):
            report.add(f"epoch_compaction_inputs.{column}", _count_nonempty_json(
                db, "epoch_compaction_inputs", column, epoch_compactions))

    # Runtime
    working = working_state_ids(db, scope)
    if working:
        report.add("working_states.current_objective", _count_nonempty_text(
            db, "working_states", "current_objective", working))
        for column in (
            "open_loops", "active_constraints", "pending_approvals", "artifact_refs",
            "verified_tool_states", "uncommitted_side_effects", "running_tool_state",
            "applied_idempotency_keys",
        ):
            report.add(f"working_states.{column}", _count_nonempty_json(
                db, "working_states", column, working))
    contexts = context_snapshot_ids(db, scope)
    if contexts:
        report.add("context_snapshots.context_items", _count_nonempty_json(
            db, "context_snapshots", "context_items", contexts))
        report.add("context_snapshots.meta", _count_nonempty_json(
            db, "context_snapshots", "meta", contexts))
    calls = llm_call_ids(db, scope)
    if calls:
        for column in ("input_preview", "output_preview"):
            report.add(f"llm_calls.{column}", _count_nonempty_text(
                db, "llm_calls", column, calls))
    notes = task_note_ids(db, scope)
    if notes:
        for column in ("title", "description", "condition"):
            report.add(f"tasks.{column}", _count_nonempty_text(
                db, "tasks", column, notes))
    artifacts = artifact_ids(db, scope)
    if artifacts:
        report.add("artifacts.content", _count_nonempty_text(
            db, "artifacts", "content", artifacts))
    attention = attention_ids(db, scope)
    if attention:
        report.add("attention_states.focus_topic", _count_nonempty_text(
            db, "attention_states", "focus_topic", attention))
        report.add("attention_states.recent_topics", _count_nonempty_json(
            db, "attention_states", "recent_topics", attention))
    title_ids = set(scope.thread_ids)
    if scope.all_user or scope.time_from is not None or scope.time_to is not None:
        tq = db.query(Thread.id).filter(Thread.created_at <= scope.cutoff)
        if scope.time_from is not None:
            tq = tq.filter(Thread.created_at >= scope.time_from)
        if scope.time_to is not None:
            tq = tq.filter(Thread.created_at <= scope.time_to)
        title_ids |= {str(r[0]) for r in tq.all() if r[0]}
    if title_ids:
        report.add("threads.title", _count_nonempty_text(
            db, "threads", "title", title_ids))

    tool_ops = tool_operation_ids(db, scope)
    if tool_ops:
        for column in ("params", "result_payload", "effect_receipt"):
            report.add(f"tool_operations.{column}", _count_nonempty_json(
                db, "tool_operations", column, tool_ops))
        report.add("tool_operations.error_message", _count_nonempty_text(
            db, "tool_operations", "error_message", tool_ops))
    approvals = approval_request_ids(db, scope)
    if approvals:
        for column in (
            "tool_args", "preconditions", "effects", "execution_result", "risk_snapshot",
        ):
            report.add(f"approval_requests.{column}", _count_nonempty_json(
                db, "approval_requests", column, approvals))
        report.add("approval_requests.error_message", _count_nonempty_text(
            db, "approval_requests", "error_message", approvals))

    # Task Runtime：父字段按父 id、子记录字段按独立子记录 id 验证
    tasks = scope.agent_task_ids
    partial = scope.partial_agent_task_ids
    if tasks:
        for label, sql in (
            ("agent_tasks.title", "SELECT COUNT(*) FROM agent_tasks WHERE id IN :ids AND title <> '[redacted]'"),
            ("agent_tasks.goal", "SELECT COUNT(*) FROM agent_tasks WHERE id IN :ids AND goal <> '[redacted]'"),
            ("agent_tasks.report", "SELECT COUNT(*) FROM agent_tasks WHERE id IN :ids AND report IS NOT NULL"),
            ("agent_tasks.last_error", "SELECT COUNT(*) FROM agent_tasks WHERE id IN :ids AND COALESCE(last_error, '') <> ''"),
        ):
            report.add(label, _sum_in(db, sql, tasks))
        for column in ("task_brief", "task_state"):
            report.add(f"agent_tasks.{column}", _count_nonempty_json(
                db, "agent_tasks", column, tasks))

    child_checks = (
        ("agent_runs.input_snapshot", "agent_runs", "input_snapshot", scope.agent_run_ids, "json_obj"),
        ("agent_runs.output_summary", "agent_runs", "output_summary", scope.agent_run_ids, "json_null"),
        ("agent_runs.error_message", "agent_runs", "error_message", scope.agent_run_ids, "text"),
        ("agent_actions.arguments", "agent_actions", "arguments", scope.agent_action_ids, "json_obj"),
        ("agent_actions.preconditions", "agent_actions", "preconditions", scope.agent_action_ids, "json_arr"),
        ("agent_actions.effects", "agent_actions", "effects", scope.agent_action_ids, "json_arr"),
        ("agent_actions.result_summary", "agent_actions", "result_summary", scope.agent_action_ids, "json_null"),
        ("agent_actions.evidence_refs", "agent_actions", "evidence_refs", scope.agent_action_ids, "json_arr"),
        ("agent_actions.error_message", "agent_actions", "error_message", scope.agent_action_ids, "text"),
        ("agent_task_events.payload", "agent_task_events", "payload", scope.agent_task_event_ids, "json_obj"),
        ("agent_task_checkpoints.state", "agent_task_checkpoints", "state", scope.agent_task_checkpoint_ids, "json_obj"),
        ("agent_task_checkpoints.plan", "agent_task_checkpoints", "plan", scope.agent_task_checkpoint_ids, "json_obj"),
        ("agent_task_checkpoints.pending_action_refs", "agent_task_checkpoints", "pending_action_refs", scope.agent_task_checkpoint_ids, "json_arr"),
        ("agent_task_checkpoints.evidence_refs", "agent_task_checkpoints", "evidence_refs", scope.agent_task_checkpoint_ids, "json_arr"),
        ("task_evidence.summary", "task_evidence", "summary", scope.task_evidence_ids, "text"),
        ("task_evidence.inline_payload", "task_evidence", "inline_payload", scope.task_evidence_ids, "json_null"),
        ("task_artifacts.name", "task_artifacts", "name", scope.task_artifact_ids, "text"),
        ("task_artifacts.summary", "task_artifacts", "summary", scope.task_artifact_ids, "text"),
    )
    for label, table, column, ids, kind in child_checks:
        if not ids:
            continue
        if kind == "json_obj":
            report.add(label, _sum_in(
                db,
                f"SELECT COUNT(*) FROM {table} WHERE id IN :ids AND {column} IS NOT NULL "
                "AND CAST(" + column + " AS TEXT) NOT IN ('{}', 'null')",
                ids,
            ))
        elif kind == "json_arr":
            report.add(label, _sum_in(
                db,
                f"SELECT COUNT(*) FROM {table} WHERE id IN :ids AND {column} IS NOT NULL "
                "AND CAST(" + column + " AS TEXT) NOT IN ('[]', 'null')",
                ids,
            ))
        elif kind == "json_null":
            report.add(label, _sum_in(
                db,
                f"SELECT COUNT(*) FROM {table} WHERE id IN :ids AND {column} IS NOT NULL",
                ids,
            ))
        else:
            report.add(label, _count_nonempty_text(db, table, column, set(ids)))
    report.add("task_evidence.object", _sum_in(
        db,
        "SELECT COUNT(*) FROM task_evidence WHERE id IN :ids AND "
        "(COALESCE(object_bucket, '') <> '' OR COALESCE(object_key, '') <> '')",
        scope.task_evidence_ids,
    ))
    report.add("task_artifacts.object", _sum_in(
        db,
        "SELECT COUNT(*) FROM task_artifacts WHERE id IN :ids AND "
        "(object_bucket <> '' OR object_key <> '')",
        scope.task_artifact_ids,
    ))
    removed = (
        scope.agent_run_ids | scope.agent_action_ids | scope.agent_task_event_ids
        | scope.agent_task_checkpoint_ids | scope.task_evidence_ids
        | scope.task_artifact_ids
    )
    if removed and partial:
        leftover = 0
        from aiive.db.models import AgentTask as _AgentTask
        for task in db.query(_AgentTask).filter(_AgentTask.id.in_(partial)).all():
            if _json_has_ref(task.task_state, removed) or _json_has_ref(task.task_brief, removed):
                leftover += 1
        report.add("agent_task_state_refs_partial", leftover)

    watch_task_ids = tasks | partial
    if watch_task_ids:
        watch_ids = _ids_from(
            db, "SELECT id FROM agent_task_watches WHERE task_id IN :ids",
            watch_task_ids,
        )
        if watch_ids:
            for column in ("config", "cursor"):
                report.add(f"agent_task_watches.{column}", _count_nonempty_json(
                    db, "agent_task_watches", column, watch_ids))
    if scope.agent_action_ids:
        report.add("desktop_action_receipts.result_payload", _sum_in(
            db,
            "SELECT COUNT(*) FROM desktop_action_receipts WHERE action_id IN :ids "
            "AND result_payload IS NOT NULL AND CAST(result_payload AS TEXT) NOT IN ('{}', 'null')",
            scope.agent_action_ids,
        ))
        report.add("desktop_action_receipts.error_message", _sum_in(
            db,
            "SELECT COUNT(*) FROM desktop_action_receipts WHERE action_id IN :ids "
            "AND COALESCE(error_message, '') <> ''",
            scope.agent_action_ids,
        ))

    # Auxiliary tables（冲突 / 召回 / 检索 / Outbox）
    cases = scope.conflict_case_ids
    if cases:
        for column in (
            "old_content", "new_content", "question", "verdict_reason",
            "decision_span", "resolution_reason",
        ):
            report.add(f"memory_conflict_cases.{column}", _count_nonempty_text(
                db, "memory_conflict_cases", column, cases))
        for column in ("proposal_payload", "evidence", "graph_evidence"):
            report.add(f"memory_conflict_cases.{column}", _count_nonempty_json(
                db, "memory_conflict_cases", column, cases))
        report.add("memory_conflict_cases.open_status", _sum_in(
            db,
            "SELECT COUNT(*) FROM memory_conflict_cases WHERE id IN :ids "
            "AND status IN ('pending','asking')",
            cases,
        ))
    recall = scope.recall_run_ids
    if recall:
        report.add("memory_recall_runs.request_query", _count_nonempty_text(
            db, "memory_recall_runs", "request_query", recall))
        for column in ("scope_context", "routes_executed"):
            report.add(f"memory_recall_runs.{column}", _count_nonempty_json(
                db, "memory_recall_runs", column, recall))
    retrieval = scope.retrieval_run_ids
    if retrieval:
        report.add("retrieval_runs.query", _count_nonempty_text(
            db, "retrieval_runs", "query", retrieval))
    if scope.outbox_job_ids:
        residue = 0
        for job in db.query(OutboxJob).filter(OutboxJob.id.in_(scope.outbox_job_ids)).all():
            if (job.error_message or "") or payload_has_content(
                job.job_type, job.payload or {}
            ):
                residue += 1
        report.add("outbox_jobs", residue)
        if scope.outbox_unparseable_ids:
            # 无法完整解析引用的 Job：已纳入范围并执行 Scrub，但引用提取不完整，
            # 必须显式记录（不阻塞终态，也不静默忽略）。
            report.warn("outbox_jobs.unparseable_refs", len(scope.outbox_unparseable_ids))

    docs = scope.document_ids
    if docs:
        report.add("documents.status", _sum_in(
            db, "SELECT COUNT(*) FROM documents WHERE id IN :ids AND status <> 'deleted'", docs,
        ))
        report.add("documents.title", _count_nonempty_text(
            db, "documents", "title", docs))
        report.add("documents.object", _sum_in(
            db,
            "SELECT COUNT(*) FROM documents WHERE id IN :ids AND "
            "(object_bucket IS NOT NULL OR object_key IS NOT NULL)",
            docs,
        ))
        report.add("chunks", _sum_in(
            db, "SELECT COUNT(*) FROM chunks WHERE document_id IN :ids", docs,
        ))
        chunk_action = db.query(ForgetAction).filter_by(
            idempotency_key=f"fo:{scope.operation_id}:knowledge_chunks",
        ).first()
        stored_chunks: list[str] = []
        if chunk_action is not None and isinstance(chunk_action.details, dict):
            raw_chunk_ids = chunk_action.details.get("chunk_ids")
            if isinstance(raw_chunk_ids, list):
                stored_chunks = [str(item) for item in raw_chunk_ids]
        if stored_chunks:
            report.add("knowledge_chunk_vectors", _sum_in(
                db,
                "SELECT COUNT(*) FROM knowledge_chunk_vectors WHERE chunk_id IN :ids",
                set(stored_chunks),
            ))

    # Objects
    actions = pending_object_actions(db, scope.operation_id)
    if actions:
        pending = sum(1 for a in actions if a.status in ("pending", "failed"))
        missing = 0
        for action in actions:
            details = action.details or {}
            bucket = str(details.get("bucket") or "")
            key = str(details.get("key") or "")
            if (
                action.status == "done" and bucket and key
                and not (action.details or {}).get("skipped_shared")
            ):
                try:
                    if object_store.exists(object_store.ObjectRef(bucket, key)):
                        missing += 1
                except Exception:
                    missing += 1
        report.object_actions["pending"] = pending
        report.object_actions["deleted"] = sum(1 for a in actions if a.status == "done")
        report.object_actions["still_present"] = missing
        if pending:
            report.failures.append(f"object deletions pending: {pending}")
        if missing:
            report.failures.append(f"objects still present after delete: {missing}")

    return report


def scrub_objects_and_verify(
    db: Session, scope: ContentScope,
) -> VerifyReport:
    """便捷入口：先 scrub 再 verify（测试/调用方用）。"""
    scrub_all(db, scope)
    return verify_scope(db, scope)


def scrub_all(db: Session, scope: ContentScope) -> dict[str, int]:
    counts: dict[str, int] = {}
    counts.update(scrub_raw_history(db, scope))
    counts.update(scrub_memory_derivations(db, scope))
    counts.update(scrub_summaries_and_checkpoints(db, scope))
    counts.update(scrub_runtime_tables(db, scope))
    counts.update(scrub_auxiliary_tables(db, scope))
    task_counts, task_refs = scrub_task_runtime(db, scope)
    counts.update(task_counts)
    knowledge_counts, doc_refs = scrub_knowledge(db, scope)
    counts.update(knowledge_counts)
    # 仅登记已无任何保留行引用的孤儿对象；共享对象不删除。
    orphan_refs = {
        (bucket, key)
        for bucket, key in (task_refs | doc_refs)
        if verify_object_shared_refs(db, bucket, key) == 0
    }
    counts["objects_queued"] = queue_object_deletions(
        db, scope.operation_id, orphan_refs,
    )
    return counts


def verify_object_shared_refs(db: Session, bucket: str, key: str) -> int:
    """统计仍引用该对象的非目标保留行（用于测试/诊断）。"""
    total = 0
    for table in ("task_evidence", "task_artifacts", "documents"):
        total += int(db.execute(sa_text(
            f"SELECT COUNT(*) FROM {table} WHERE object_bucket = :b AND object_key = :k"
        ), {"b": bucket, "k": key}).scalar() or 0)
    return total
