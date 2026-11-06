"""Job 来源可见性守卫：外部调用/副作用前统一 fail-closed 检查。

Phase A 提交后 Shield 立即生效，但旧 Outbox Job 的 payload 仍在内存里。
`forget_content_redacted` 只在 Purge 阶段写入，不能作为唯一判断依据，因此每个
会读取用户内容 / 调用模型 / 执行副作用的 Handler 都必须在外部调用前调用
`guard_job_sources_visible()`：

    共享执行授权 → 严格重读 OutboxJob → 提取类型化来源 ID →
    检查 active Shield / Tombstone → 全部通过才继续

按 Job 类型解析来源（无法确定来源即 fail-closed，绝不静默继续）：
  - goal_event_dispatch：event_id / thread_id
  - goal_trigger_decision：trigger_event_id / task_id / trigger_event_payload 内来源
  - reminder_delivery：task_id（含来源 Event）/ thread_id
  - segment_sealing：CompactionInput.event_manifest / turn_manifest
  - epoch_checkpoint：EpochCompactionInput → SegmentSummary.source_event_ids
  - memory_conflict_question|reply：conflict_case_id → 旧/新 Memory + proposal 来源
  - memory_vector_refresh / core_memory_refresh / memory_graph_refresh：memory_id
  - memory_markdown_project：memory_id 或 forget_operation_id（全量投影逐条过滤）
  - retrieval_index_refresh：source_type + source_id
  - agent_task_run：task → thread / current_run / source_event
  - tool_operation：tool_operation_id → turn_record_id / thread_id
  - improvement_discovery：task_id / action_id
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from aiive.forget.visibility_service import ForgetVisibilityService
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome, HandlerResult

logger = logging.getLogger(__name__)

#: 被来源屏蔽而收敛的 Job 统一原因串（COMPLETED，不重试、不产生派生内容）。
BLOCKED_REASON = "forget_source_blocked"


@dataclass
class JobSources:
    """Job 的类型化来源引用。"""

    events: dict[str, datetime | None] = field(default_factory=dict)
    turns: dict[str, datetime | None] = field(default_factory=dict)
    memories: dict[str, datetime | None] = field(default_factory=dict)
    summaries: dict[str, datetime | None] = field(default_factory=dict)
    checkpoints: dict[str, datetime | None] = field(default_factory=dict)
    segments: dict[str, datetime | None] = field(default_factory=dict)
    epochs: dict[str, datetime | None] = field(default_factory=dict)
    thread_ids: set[str] = field(default_factory=set)
    #: 非空表示来源无法可靠解析 → fail-closed。
    unresolved: str = ""

    @property
    def empty(self) -> bool:
        return not any((self.events, self.turns, self.memories, self.summaries,
                        self.checkpoints, self.segments, self.epochs,
                        self.thread_ids))


def _add(payload: Any, key: str, out: dict[str, datetime | None]) -> None:
    value = payload.get(key) if isinstance(payload, dict) else None
    if isinstance(value, str) and value:
        out.setdefault(value, None)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item:
                out.setdefault(item, None)


def _collect_manifest_ids(manifest: Any, key: str) -> list[str]:
    ids: list[str] = []
    if not isinstance(manifest, list):
        return ids
    for item in manifest:
        if isinstance(item, dict):
            value = item.get(key)
            if isinstance(value, str) and value:
                ids.append(value)
    return ids

def _resolve_from_payload(claimed: ClaimedJob, sources: JobSources) -> None:
    """先收集 payload 自带的精确引用（不依赖 DB 补充）。"""
    payload: Any = claimed.payload or {}
    if not isinstance(payload, dict):
        sources.unresolved = "payload_not_mapping"
        return
    _add(payload, "event_id", sources.events)
    _add(payload, "trigger_event_id", sources.events)
    _add(payload, "assistant_event_id", sources.events)
    _add(payload, "request_event_id", sources.events)
    _add(payload, "message_event_id", sources.events)
    _add(payload, "source_event_ids", sources.events)
    _add(payload, "event_ids", sources.events)
    _add(payload, "turn_id", sources.turns)
    _add(payload, "turn_record_id", sources.turns)
    _add(payload, "source_turn_id", sources.turns)
    _add(payload, "source_turn_record_id", sources.turns)
    _add(payload, "memory_id", sources.memories)
    _add(payload, "old_memory_id", sources.memories)
    _add(payload, "resolved_memory_id", sources.memories)
    thread_id = payload.get("thread_id")
    if isinstance(thread_id, str) and thread_id:
        sources.thread_ids.add(thread_id)


def _resolve_segment_sealing(db: Session, claimed: ClaimedJob, sources: JobSources) -> None:
    from aiive.db.models import CompactionInput

    payload = claimed.payload or {}
    input_id = str(payload.get("compaction_input_id") or "")
    segment_id = str(payload.get("segment_id") or "")
    if segment_id:
        sources.segments.setdefault(segment_id, None)
    if not input_id:
        sources.unresolved = "segment_sealing_missing_compaction_input"
        return
    ci = db.get(CompactionInput, input_id)
    if ci is None:
        sources.unresolved = "compaction_input_missing"
        return
    for event_id in _collect_manifest_ids(ci.event_manifest, "event_id"):
        sources.events.setdefault(event_id, None)
    for turn_id in _collect_manifest_ids(ci.turn_manifest, "turn_id"):
        sources.turns.setdefault(turn_id, None)
    for turn_id in _collect_manifest_ids(ci.turn_manifest, "turn_record_id"):
        sources.turns.setdefault(turn_id, None)
    if not sources.events and not sources.turns:
        sources.unresolved = "segment_sealing_empty_manifest"


def _resolve_epoch_checkpoint(db: Session, claimed: ClaimedJob, sources: JobSources) -> None:
    from aiive.db.models import EpochCompactionInput, SegmentSummary

    payload = claimed.payload or {}
    epoch_id = str(payload.get("epoch_id") or "")
    if epoch_id:
        sources.epochs.setdefault(epoch_id, None)
    input_id = str(payload.get("epoch_compaction_input_id") or "")
    if not input_id:
        sources.unresolved = "epoch_checkpoint_missing_input"
        return
    eci = db.get(EpochCompactionInput, input_id)
    if eci is None:
        sources.unresolved = "epoch_compaction_input_missing"
        return
    segment_ids = [s for s in (eci.source_segment_ids or []) if isinstance(s, str)]
    for segment_id in segment_ids:
        sources.segments.setdefault(segment_id, None)
    if not segment_ids:
        sources.unresolved = "epoch_checkpoint_empty_sources"
        return
    rows = db.query(SegmentSummary).filter(
        SegmentSummary.segment_id.in_(segment_ids),
    ).all()
    for row in rows:
        sources.summaries.setdefault(row.id, row.created_at)
        for event_id in (row.source_event_ids or []):
            if isinstance(event_id, str):
                sources.events.setdefault(event_id, None)
        for turn_id in (row.source_turn_ids or []):
            if isinstance(turn_id, str):
                sources.turns.setdefault(turn_id, None)
    if not sources.summaries and not sources.events and not sources.turns:
        sources.unresolved = "epoch_checkpoint_no_summary_sources"


def _resolve_conflict_case(db: Session, claimed: ClaimedJob, sources: JobSources) -> None:
    from aiive.db.models import MemoryConflictCase, MemoryProposal

    payload = claimed.payload or {}
    case_id = str(payload.get("conflict_case_id") or "")
    if not case_id:
        sources.unresolved = "conflict_case_missing_id"
        return
    case = db.get(MemoryConflictCase, case_id)
    if case is None:
        sources.unresolved = "conflict_case_missing"
        return
    if case.thread_id:
        sources.thread_ids.add(case.thread_id)
    for memory_id in (case.old_memory_id, case.resolved_memory_id):
        if isinstance(memory_id, str) and memory_id:
            sources.memories.setdefault(memory_id, None)
    if case.proposal_id:
        proposal = db.get(MemoryProposal, case.proposal_id)
        if proposal is not None:
            for raw_event_id in (proposal.source_event_ids or []):
                sources.events.setdefault(str(raw_event_id), None)
            if proposal.source_turn_id:
                sources.turns.setdefault(proposal.source_turn_id, None)
            if proposal.source_turn_record_id:
                sources.turns.setdefault(proposal.source_turn_record_id, None)


def _resolve_task(db: Session, claimed: ClaimedJob, sources: JobSources) -> None:
    from aiive.db.models import AgentTask

    payload = claimed.payload or {}
    task_id = str(payload.get("task_id") or "")
    if not task_id:
        sources.unresolved = "task_id_missing"
        return
    task = db.get(AgentTask, task_id)
    if task is None:
        sources.unresolved = "agent_task_missing"
        return
    if task.thread_id:
        sources.thread_ids.add(task.thread_id)
    source_event_id = getattr(task, "source_event_id", None)
    if isinstance(source_event_id, str) and source_event_id:
        sources.events.setdefault(source_event_id, None)
    # AgentTask 的 Turn 来源（Task Runtime 在 run/action 中沿用该 Turn 内容）
    source_turn = getattr(task, "source_turn_record_id", None)
    if isinstance(source_turn, str) and source_turn:
        sources.turns.setdefault(source_turn, None)


def _resolve_reminder_task(db: Session, claimed: ClaimedJob, sources: JobSources) -> None:
    """提醒任务来自 tasks 表（与 agent_tasks 不同模型）。"""
    from aiive.db.models import Task

    payload = claimed.payload or {}
    task_id = str(payload.get("task_id") or "")
    if not task_id:
        sources.unresolved = "reminder_task_id_missing"
        return
    task = db.get(Task, task_id)
    if task is None:
        sources.unresolved = "reminder_task_missing"
        return
    if task.thread_id:
        sources.thread_ids.add(task.thread_id)
    source_event_id = getattr(task, "source_event_id", None)
    if isinstance(source_event_id, str) and source_event_id:
        sources.events.setdefault(source_event_id, None)


def _resolve_tool_operation(db: Session, claimed: ClaimedJob, sources: JobSources) -> None:
    from aiive.db.models import ToolOperation

    payload = claimed.payload or {}
    op_id = str(payload.get("tool_operation_id") or "")
    if not op_id:
        sources.unresolved = "tool_operation_id_missing"
        return
    op = db.get(ToolOperation, op_id)
    if op is None:
        sources.unresolved = "tool_operation_missing"
        return
    if op.thread_id:
        sources.thread_ids.add(op.thread_id)
    if op.turn_record_id:
        sources.turns.setdefault(op.turn_record_id, None)
    if op.turn_id:
        sources.turns.setdefault(op.turn_id, None)


def _resolve_retrieval_refresh(_db: Session, claimed: ClaimedJob, sources: JobSources) -> None:
    payload = claimed.payload or {}
    source_type = str(payload.get("source_type") or "")
    source_id = str(payload.get("source_id") or "")
    if not source_type or not source_id:
        sources.unresolved = "retrieval_refresh_missing_source"
        return
    mapping = {
        "memory": sources.memories,
        "memory_record": sources.memories,
        "segment": sources.segments,
        "epoch": sources.epochs,
        "summary": sources.summaries,
        "segment_summary": sources.summaries,
        "checkpoint": sources.checkpoints,
        "epoch_checkpoint": sources.checkpoints,
    }
    bucket = mapping.get(source_type)
    if bucket is None:
        # 未知 source_type 时无法判断实体归属 → fail-closed
        sources.unresolved = f"retrieval_refresh_unknown_source_type:{source_type}"
        return
    bucket.setdefault(source_id, None)


_RESOLVERS = {
    "segment_sealing": _resolve_segment_sealing,
    "epoch_checkpoint": _resolve_epoch_checkpoint,
    "memory_conflict_question": _resolve_conflict_case,
    "memory_conflict_reply": _resolve_conflict_case,
    "agent_task_run": _resolve_task,
    "reminder_delivery": _resolve_reminder_task,
    "goal_trigger_decision": _resolve_task,
    "tool_operation": _resolve_tool_operation,
    "retrieval_index_refresh": _resolve_retrieval_refresh,
}

#: 不含外部用户内容的内部 Job（Forget Saga / 保留期清理 / 知识库一致性维护）。
#: 它们的 payload 只有 ID 与状态，不读取用户内容，也不调用模型。
_NO_EXTERNAL_SOURCE_JOB_TYPES: frozenset[str] = frozenset({
    "forget_cascade", "forget_rebuild_dependencies", "forget_purge",
    "forget_verify", "forget_reconcile", "retention_cleanup",
    "knowledge_maintenance", "memory_conflict_cleanup",
})

#: 没有单一来源、按条目/证据逐条过滤的 Job：只做 claim fencing，
#: 被屏蔽内容由各自服务（检索重建 / 证据收集）逐条剔除。
_CLAIM_ONLY_JOB_TYPES: frozenset[str] = frozenset({
    "retrieval_index_rebuild", "improvement_discovery",
})

#: Memory 派生投影：payload 的 memory_id 已进入来源集合；为空说明载荷非法，
#: 交由 Handler 自身校验返回 invalid_payload（没有内容可泄漏）。
_ALLOW_EMPTY_SOURCE_JOB_TYPES: frozenset[str] = frozenset({
    "memory_vector_refresh", "core_memory_refresh", "memory_graph_refresh",
    "memory_markdown_project", "memory_maintenance",
})


def resolve_job_sources(db: Session, claimed: ClaimedJob) -> JobSources:
    """解析 Job 的类型化来源引用（payload + DB 补充）。"""
    sources = JobSources()
    _resolve_from_payload(claimed, sources)
    raw_payload: Any = claimed.payload
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    trigger_payload = payload.get("trigger_event_payload")
    if isinstance(trigger_payload, dict):
        # 触发事件正文里可能带自己的来源 Event / Turn
        for key in ("event_id", "turn_id", "turn_record_id", "source_turn_record_id"):
            value = trigger_payload.get(key)
            if isinstance(value, str) and value:
                (sources.events if "event" in key else sources.turns).setdefault(value, None)
    resolver = _RESOLVERS.get(claimed.job_type)
    if resolver is not None:
        resolver(db, claimed, sources)
    return sources


def _blocked_ids(
    db: Session, target_type: str, ids: dict[str, datetime | None],
) -> set[str]:
    if not ids:
        return set()
    return ForgetVisibilityService.blocked_target_ids(
        db, target_type, list(ids.items()),
    )


def blocked_source_reason(
    db: Session, _claimed: ClaimedJob, sources: JobSources,
    job_created_at: datetime | None = None,
) -> str:
    """返回屏蔽原因；空串表示来源可见。"""
    if sources.unresolved:
        return sources.unresolved
    # 只使用 ForgetVisibilityService 支持元数据重载的目标类型；segment / epoch
    # 的屏蔽通过其 Summary / Checkpoint 与 Thread 选择器传递，不在本层重复判定
    # （未知 created_at 会被 all_user_data 分支 fail-closed 过度拦截）。
    groups = (
        ("event", sources.events),
        ("turn_record", sources.turns),
        ("memory_record", sources.memories),
        ("segment_summary", sources.summaries),
        ("epoch_checkpoint", sources.checkpoints),
    )
    for target_type, ids in groups:
        if _blocked_ids(db, target_type, ids):
            return f"blocked:{target_type}"
    for thread_id in sorted(sources.thread_ids):
        if ForgetVisibilityService.is_shielded(
            db, thread_id=thread_id, target_time=job_created_at,
        ):
            return "blocked:thread"
    return ""


def guard_job_sources_visible(
    claimed: ClaimedJob, session_factory: Any = None,
) -> tuple[ClaimedJob | None, HandlerResult | None]:
    """外部调用/副作用前的统一守卫。

    必须在共享执行授权内调用：先严格校验 claim（行存在 / running / token /
    owner / lease），再解析类型化来源并检查 active Shield / Tombstone。
    任一失败都返回终止结果，调用方不得再读取内容或调用模型。
    """
    from aiive.db.models import OutboxJob
    from aiive.worker.outbox_execution_auth import (
        get_session_factory,
        session_is_ours,
        verify_claim,
    )

    factory = session_factory or get_session_factory()
    db = factory()
    owns = session_is_ours(db)
    try:
        fresh, reason = verify_claim(db, claimed)
        if fresh is None:
            if reason == "forget_content_redacted":
                return None, HandlerResult(HandlerOutcome.COMPLETED, reason)
            return None, HandlerResult(HandlerOutcome.CLAIM_LOST, reason)
        if (
            fresh.job_type in _NO_EXTERNAL_SOURCE_JOB_TYPES
            or fresh.job_type in _CLAIM_ONLY_JOB_TYPES
        ):
            return fresh, None
        job_created = db.query(OutboxJob.created_at).filter(
            OutboxJob.id == fresh.id,
        ).scalar()
        sources = resolve_job_sources(db, fresh)
        if sources.empty and not sources.unresolved:
            # memory_maintenance 由 MaintenanceRun 快照驱动（payload 只有 operation_id），
            # 候选在写入前仍逐条经 lifecycle / 可见性过滤，因此不因「无来源 ID」阻断。
            if fresh.job_type not in _ALLOW_EMPTY_SOURCE_JOB_TYPES:
                sources.unresolved = "no_source_reference"
        reason = blocked_source_reason(db, fresh, sources, job_created)
        if reason:
            logger.info(
                "Outbox Job 来源已屏蔽，终止执行: job=%s type=%s reason=%s",
                fresh.id, fresh.job_type, reason,
            )
            return None, HandlerResult(
                HandlerOutcome.COMPLETED, f"{BLOCKED_REASON}:{reason}",
            )
        return fresh, None
    finally:
        if owns:
            db.close()