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
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from collections.abc import Iterable
from typing import Any

from sqlalchemy import bindparam, text as sa_text
from sqlalchemy.orm import Session

from aiive.db.forget_models import ForgetAction
from aiive.db.models import Document, TaskArtifact, TaskEvidence
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
        ids |= _ids_from(db, f"SELECT id FROM {table} WHERE thread_id IN :ids", scope.thread_ids)
    if scope.all_user:
        ids |= {str(r[0]) for r in db.execute(sa_text(f"SELECT id FROM {table}")).all() if r[0]}
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if scope.time_from is not None:
        clauses.append(f"{time_column} >= :tf")
        params["tf"] = scope.time_from
    if scope.time_to is not None:
        clauses.append(f"{time_column} <= :tt")
        params["tt"] = scope.time_to
    if clauses:
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
    return _thread_or_time_ids(db, "llm_calls", "created_at", scope)


def task_note_ids(db: Session, scope: ContentScope) -> set[str]:
    return _thread_or_time_ids(db, "tasks", "created_at", scope)


def attention_ids(db: Session, scope: ContentScope) -> set[str]:
    return _thread_or_time_ids(db, "attention_states", "created_at", scope)


def inbox_ids(db: Session, scope: ContentScope) -> set[str]:
    return _thread_or_time_ids(db, "turn_event_inbox", "created_at", scope)


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
    if scope.thread_ids:
        ids |= _ids_from(db, "SELECT id FROM retrieval_index_entries WHERE thread_id IN :ids", scope.thread_ids)
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if scope.time_from is not None:
        clauses.append("created_source_at >= :tf")
        params["tf"] = scope.time_from
    if scope.time_to is not None:
        clauses.append("created_source_at <= :tt")
        params["tt"] = scope.time_to
    if clauses:
        rows = db.execute(sa_text(
            "SELECT id FROM retrieval_index_entries WHERE " + " AND ".join(clauses)
        ), params).all()
        ids |= {str(r[0]) for r in rows}
    if scope.all_user:
        ids |= {str(r[0]) for r in db.execute(sa_text("SELECT id FROM retrieval_index_entries")).all()}
    return ids


# ═══════════════════════════════════════════════════════════════════
# Scrub
# ═══════════════════════════════════════════════════════════════════

def scrub_raw_history(db: Session, scope: ContentScope) -> dict[str, int]:
    counts: dict[str, int] = {}
    if scope.preserve_raw_history:
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
        counts["retrieval_index_entries"] = _execute_ids(
            db,
            "UPDATE retrieval_index_entries SET title = '[forgotten]', search_text = '', "
            "snippet = '', metadata = '{}', is_searchable = false WHERE id IN :ids",
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
    return counts


def scrub_task_runtime(
    db: Session, scope: ContentScope,
) -> tuple[dict[str, int], set[tuple[str, str]]]:
    """清理 Agent Task Runtime，返回计数与待删除对象引用。"""
    counts: dict[str, int] = {}
    refs: set[tuple[str, str]] = set()
    task_ids = scope.agent_task_ids
    if not task_ids:
        return counts, refs

    # 运行中的任务转为合法终态，避免恢复逻辑读取已脱敏的 task_state 后继续执行。
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
    counts["agent_runs"] = _execute_ids(
        db,
        "UPDATE agent_runs SET input_snapshot = '{}', output_summary = NULL, "
        "error_message = NULL WHERE task_id IN :ids",
        task_ids,
    )
    counts["agent_actions"] = _execute_ids(
        db,
        "UPDATE agent_actions SET arguments = '{}', preconditions = '[]', effects = '[]', "
        "result_summary = NULL, evidence_refs = '[]', error_message = NULL "
        "WHERE task_id IN :ids",
        task_ids,
    )
    counts["agent_task_events"] = _execute_ids(
        db, "UPDATE agent_task_events SET payload = '{}' WHERE task_id IN :ids", task_ids,
    )
    counts["agent_task_checkpoints"] = _execute_ids(
        db,
        "UPDATE agent_task_checkpoints SET state = '{}', plan = '{}', "
        "pending_action_refs = '[]', evidence_refs = '[]' WHERE task_id IN :ids",
        task_ids,
    )

    for bucket, key in db.query(TaskEvidence.object_bucket, TaskEvidence.object_key).filter(
        TaskEvidence.task_id.in_(task_ids),
        TaskEvidence.object_bucket.isnot(None),
        TaskEvidence.object_key.isnot(None),
    ).all():
        if bucket and key:
            refs.add((str(bucket), str(key)))
    counts["task_evidence"] = _execute_ids(
        db,
        "UPDATE task_evidence SET summary = '[redacted]', inline_payload = NULL, "
        "object_bucket = NULL, object_key = NULL WHERE task_id IN :ids",
        task_ids,
    )

    for bucket, key in db.query(TaskArtifact.object_bucket, TaskArtifact.object_key).filter(
        TaskArtifact.task_id.in_(task_ids),
        TaskArtifact.object_key != "",
    ).all():
        if bucket and key:
            refs.add((str(bucket), str(key)))
    counts["task_artifacts"] = _execute_ids(
        db,
        "UPDATE task_artifacts SET name = '[redacted]', summary = '[redacted]', "
        "object_bucket = '', object_key = '' WHERE task_id IN :ids",
        task_ids,
    )
    return counts, refs


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
        }

    def add(self, label: str, count: int) -> None:
        if count:
            self.residues[label] = count
            self.failures.append(f"{label}: {count} residue rows")


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
    report.unknown = count_unknown_json_shapes(db)
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
        inbox = inbox_ids(db, scope)
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
    tool_ops = tool_operation_ids(db, scope)
    if tool_ops:
        report.add("tool_operations.params", _count_nonempty_json(
            db, "tool_operations", "params", tool_ops))
        report.add("tool_operations.result_payload", _count_nonempty_json(
            db, "tool_operations", "result_payload", tool_ops))
    approvals = approval_request_ids(db, scope)
    if approvals:
        report.add("approval_requests.tool_args", _count_nonempty_json(
            db, "approval_requests", "tool_args", approvals))
        report.add("approval_requests.execution_result", _count_nonempty_json(
            db, "approval_requests", "execution_result", approvals))

    # Task Runtime
    tasks = scope.agent_task_ids
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
        for label, sql in (
            ("agent_runs.input_snapshot", "SELECT COUNT(*) FROM agent_runs WHERE task_id IN :ids AND input_snapshot IS NOT NULL AND CAST(input_snapshot AS TEXT) NOT IN ('{}', 'null')"),
            ("agent_runs.output_summary", "SELECT COUNT(*) FROM agent_runs WHERE task_id IN :ids AND output_summary IS NOT NULL"),
            ("agent_actions.arguments", "SELECT COUNT(*) FROM agent_actions WHERE task_id IN :ids AND arguments IS NOT NULL AND CAST(arguments AS TEXT) NOT IN ('{}', 'null')"),
            ("agent_actions.result_summary", "SELECT COUNT(*) FROM agent_actions WHERE task_id IN :ids AND result_summary IS NOT NULL"),
            ("agent_task_events.payload", "SELECT COUNT(*) FROM agent_task_events WHERE task_id IN :ids AND payload IS NOT NULL AND CAST(payload AS TEXT) NOT IN ('{}', 'null')"),
            ("task_evidence.summary", "SELECT COUNT(*) FROM task_evidence WHERE task_id IN :ids AND summary <> '[redacted]'"),
            ("task_evidence.object", "SELECT COUNT(*) FROM task_evidence WHERE task_id IN :ids AND (COALESCE(object_bucket, '') <> '' OR COALESCE(object_key, '') <> '')"),
            ("task_artifacts.summary", "SELECT COUNT(*) FROM task_artifacts WHERE task_id IN :ids AND summary <> '[redacted]'"),
            ("task_artifacts.object", "SELECT COUNT(*) FROM task_artifacts WHERE task_id IN :ids AND (object_bucket <> '' OR object_key <> '')"),
        ):
            report.add(label, _sum_in(db, sql, tasks))
        for label, sql in (
            ("agent_task_checkpoints.state", "SELECT COUNT(*) FROM agent_task_checkpoints WHERE task_id IN :ids AND state IS NOT NULL AND CAST(state AS TEXT) NOT IN ('{}', 'null')"),
            ("agent_task_checkpoints.plan", "SELECT COUNT(*) FROM agent_task_checkpoints WHERE task_id IN :ids AND plan IS NOT NULL AND CAST(plan AS TEXT) NOT IN ('{}', 'null')"),
        ):
            report.add(label, _sum_in(db, sql, tasks))

    # Knowledge
    docs = scope.document_ids
    if docs:
        report.add("documents.status", _sum_in(
            db, "SELECT COUNT(*) FROM documents WHERE id IN :ids AND status <> 'deleted'", docs,
        ))
        report.add("documents.object", _sum_in(
            db,
            "SELECT COUNT(*) FROM documents WHERE id IN :ids AND "
            "(object_bucket IS NOT NULL OR object_key IS NOT NULL)",
            docs,
        ))
        chunk_ids = document_chunk_ids(db, docs)
        report.add("chunks", _sum_in(
            db, "SELECT COUNT(*) FROM chunks WHERE document_id IN :ids", docs,
        ))
        if chunk_ids:
            report.add("knowledge_chunk_vectors", _sum_in(
                db, "SELECT COUNT(*) FROM knowledge_chunk_vectors WHERE chunk_id IN :ids",
                chunk_ids,
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
