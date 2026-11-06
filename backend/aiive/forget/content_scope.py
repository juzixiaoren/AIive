# pyright: reportImplicitStringConcatenation=false
"""Forget 宽范围内容范围解析。

把 SelectorManifest 解析成一组目标 ID / 时间窗口，供 Scrub 与 Verify 共用。
原则：
  - 不按表名猜测关系，所有关联都基于 ORM 中真实存在的列/JSON 引用；
  - 不在 SQL 中用 LIKE 判断 JSON 数组成员；
  - memory_only 保留 Event/Turn 原始正文，但派生内容仍纳入范围。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import bindparam, text as sa_text
from sqlalchemy.orm import Session

from aiive.worker.outbox_payload_contract import (
    is_internal_job,
    job_reference_ids,
    operation_id_tokens,
)
from aiive.db.forget_models import (
    ForgetSelectorManifest,
    ForgetTarget,
)
from aiive.db.models import (
    AgentAction,
    AgentRun,
    AgentTask,
    AgentTaskCheckpoint,
    AgentTaskEvent,
    Chunk,
    Document,
    Epoch,
    EpochCheckpoint,
    Event,
    MemoryConflictCase as ConflictCase,
    MemoryEvidence,
    MemoryProposal,
    MemoryRecallCandidate,
    MemoryRecallRun,
    MemoryRecord,
    RetrievalRun,
    Segment,
    SegmentSummary,
    TaskArtifact,
    TaskEvidence,
    TurnRecord,
)
from aiive.db.forget_models import ContentProvenanceRef

WIDE_SELECTORS = frozenset({
    "thread", "time_range", "all_user_data", "canonical_key", "scope",
})


@dataclass
class ContentScope:
    """一次 Forget 操作实际覆盖的内容范围。"""

    operation_id: str
    mode: str
    selector_type: str
    cutoff: datetime
    thread_ids: set[str] = field(default_factory=set)
    time_from: datetime | None = None
    time_to: datetime | None = None
    all_user: bool = False
    memory_ids: set[str] = field(default_factory=set)
    # 用于匹配派生副本（proposal/summary）的来源集合，含 memory 来源 Event。
    event_ids: set[str] = field(default_factory=set)
    turn_tokens: set[str] = field(default_factory=set)
    # 仅由 Selector 直接命中的原始历史；memory_ids/canonical/scope 不直接清历史，
    # 其独占来源 Event 由 _scrub_batch 处理。
    raw_event_ids: set[str] = field(default_factory=set)
    raw_turn_tokens: set[str] = field(default_factory=set)
    segment_ids: set[str] = field(default_factory=set)
    epoch_ids: set[str] = field(default_factory=set)
    agent_task_ids: set[str] = field(default_factory=set)
    # time_range 下只命中部分子记录时的父 Task（不整体清空父历史）。
    partial_agent_task_ids: set[str] = field(default_factory=set)
    agent_run_ids: set[str] = field(default_factory=set)
    agent_action_ids: set[str] = field(default_factory=set)
    agent_task_event_ids: set[str] = field(default_factory=set)
    agent_task_checkpoint_ids: set[str] = field(default_factory=set)
    task_evidence_ids: set[str] = field(default_factory=set)
    task_artifact_ids: set[str] = field(default_factory=set)
    trace_ids: set[str] = field(default_factory=set)
    conflict_case_ids: set[str] = field(default_factory=set)
    tool_operation_ids: set[str] = field(default_factory=set)
    recall_run_ids: set[str] = field(default_factory=set)
    retrieval_run_ids: set[str] = field(default_factory=set)
    outbox_job_ids: set[str] = field(default_factory=set)
    outbox_unparseable_ids: set[str] = field(default_factory=set)
    summary_ids: set[str] = field(default_factory=set)
    checkpoint_ids: set[str] = field(default_factory=set)
    document_ids: set[str] = field(default_factory=set)

    @property
    def preserve_raw_history(self) -> bool:
        return self.mode == "memory_only"

    @property
    def is_wide(self) -> bool:
        return self.selector_type in WIDE_SELECTORS


def parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return None


def normalize_turn_tokens(db: Session, turn_ids: list[str]) -> set[str]:
    """把 TurnRecord.id / 逻辑 turn_id 统一展开为两种写法。"""
    ids = [tid for tid in turn_ids if tid]
    if not ids:
        return set()
    rows = db.query(TurnRecord.id, TurnRecord.turn_id).filter(
        TurnRecord.id.in_(ids) | TurnRecord.turn_id.in_(ids)
    ).all()
    tokens: set[str] = set(ids)
    for row in rows:
        if row[0]:
            tokens.add(str(row[0]))
        if row[1]:
            tokens.add(str(row[1]))
    return tokens


def select_json_array_members(
    db: Session, table: str, id_col: str, array_col: str, ids: list[str],
) -> list[str]:
    """返回 JSON 数组字段与 ids 有交集的记录 id（批量，一次查询）。

    PostgreSQL 使用 jsonb_array_elements_text，SQLite 使用 json1 json_each；
    其他方言退化为 Python 精确成员判断（绝不用 LIKE）。
    """
    unique = list(dict.fromkeys(str(i) for i in ids if i))
    if not unique:
        return []
    dialect = db.get_bind().dialect.name
    matched: list[str] = []
    for start in range(0, len(unique), 400):
        chunk = unique[start:start + 400]
        expand = bindparam("ids", expanding=True)
        if dialect == "postgresql":
            sql = sa_text(
                f"SELECT {id_col} FROM {table} WHERE "
                f"jsonb_typeof(CAST({array_col} AS jsonb)) = 'array' AND EXISTS ("
                f"SELECT 1 FROM jsonb_array_elements_text(CAST({array_col} AS jsonb)) AS je(value) "
                "WHERE je.value IN :ids)"
            ).bindparams(expand)
            rows = db.execute(sql, {"ids": chunk}).all()
        elif dialect == "sqlite":
            sql = sa_text(
                f"SELECT {id_col} FROM {table} WHERE json_type({array_col}) = 'array' AND EXISTS ("
                f"SELECT 1 FROM json_each({array_col}) AS je WHERE je.value IN :ids)"
            ).bindparams(expand)
            rows = db.execute(sql, {"ids": chunk}).all()
        else:  # pragma: no cover - 仅 PG/SQLite 为受支持后端
            rows = db.execute(sa_text(f"SELECT {id_col}, {array_col} FROM {table}")).all()
            wanted = set(chunk)
            rows = [
                row for row in rows
                if isinstance(row[1], list) and any(str(item) in wanted for item in row[1])
            ]
        matched.extend(str(r[0]) for r in rows if r[0])
    return list(dict.fromkeys(matched))


def count_json_array_members(
    db: Session, table: str, array_col: str, ids: list[str],
) -> int:
    """统计 JSON 数组字段与 ids 有交集的记录数（Verify 用）。"""
    unique = list(dict.fromkeys(str(i) for i in ids if i))
    if not unique:
        return 0
    dialect = db.get_bind().dialect.name
    total = 0
    for start in range(0, len(unique), 400):
        chunk = unique[start:start + 400]
        expand = bindparam("ids", expanding=True)
        if dialect == "postgresql":
            sql = sa_text(
                f"SELECT COUNT(*) FROM {table} WHERE "
                f"jsonb_typeof(CAST({array_col} AS jsonb)) = 'array' AND EXISTS ("
                f"SELECT 1 FROM jsonb_array_elements_text(CAST({array_col} AS jsonb)) AS je(value) "
                "WHERE je.value IN :ids)"
            ).bindparams(expand)
        elif dialect == "sqlite":
            sql = sa_text(
                f"SELECT COUNT(*) FROM {table} WHERE json_type({array_col}) = 'array' AND EXISTS ("
                f"SELECT 1 FROM json_each({array_col}) AS je WHERE je.value IN :ids)"
            ).bindparams(expand)
        else:  # pragma: no cover
            ids_set = set(chunk)
            rows = db.execute(sa_text(f"SELECT {array_col} FROM {table}")).all()
            total += sum(
                1 for r in rows
                if isinstance(r[0], list) and any(str(v) in ids_set for v in r[0])
            )
            continue
        total += int(db.execute(sql, {"ids": chunk}).scalar() or 0)
    return total


def _ids_from_query(_db: Session, query: Any) -> set[str]:
    return {str(row[0]) for row in query.all() if row and row[0]}


def _time_filter(column: Any, scope_from: datetime | None, scope_to: datetime | None) -> list[Any]:
    filters: list[Any] = []
    if scope_from is not None:
        filters.append(column >= scope_from)
    if scope_to is not None:
        filters.append(column <= scope_to)
    return filters


def _as_utc(value: Any) -> datetime | None:
    """SQLite raw SQL 可能返回 ISO 字符串，PostgreSQL 返回 datetime。"""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _discover_outbox_jobs(
    db: Session, scope: "ContentScope",
) -> tuple[set[str], set[str]]:
    """精确、有界地发现目标 Outbox Job。

    按 id keyset 分批读取（每批 500 行，不一次性加载全表）；用
    outbox_payload_contract 的结构化引用 ID 做精确集合匹配，禁用 JSON 全文
    LIKE / 子串包含。返回 (命中 Job ID, 无法可靠解析的 Job ID)。
    """
    target_events = set(scope.event_ids) | set(scope.raw_event_ids)
    target_turns = set(scope.turn_tokens)
    target_memories = set(scope.memory_ids)
    target_children = (
        scope.agent_run_ids | scope.agent_action_ids | scope.agent_task_event_ids
        | scope.agent_task_checkpoint_ids | scope.task_evidence_ids
        | scope.task_artifact_ids | scope.segment_ids | scope.epoch_ids
        | scope.summary_ids | scope.checkpoint_ids | scope.document_ids
        | scope.conflict_case_ids | scope.tool_operation_ids
    )
    target_full_tasks = set(scope.agent_task_ids)
    target_threads = set(scope.thread_ids)
    target_traces = set(scope.trace_ids)
    # retrieval_index_refresh 的 source_id 必须结合 source_type 判定实体类型。
    source_sets: dict[str, set[str]] = {
        "memory": target_memories,
        "memory_record": target_memories,
        "segment": set(scope.segment_ids),
        "epoch": set(scope.epoch_ids),
        "summary": set(scope.summary_ids),
        "segment_summary": set(scope.summary_ids),
        "checkpoint": set(scope.checkpoint_ids),
        "epoch_checkpoint": set(scope.checkpoint_ids),
        "document": set(scope.document_ids),
        "task": target_full_tasks,
        "tool_operation": set(scope.tool_operation_ids),
    }
    cutoff = _as_utc(scope.cutoff)
    tf = _as_utc(scope.time_from)
    tt = _as_utc(scope.time_to)
    matched: set[str] = set()
    unparseable: set[str] = set()
    last_id = ""
    batch_size = 500
    while True:
        rows = db.execute(sa_text(
            "SELECT id, job_type, payload, trace_id, operation_id, created_at "
            "FROM outbox_jobs WHERE id > :last ORDER BY id LIMIT :limit"
        ), {"last": last_id, "limit": batch_size}).all()
        if not rows:
            break
        last_id = str(rows[-1][0])
        for job_id, job_type, payload, trace_id, operation_id, created_at in rows:
            job_id = str(job_id)
            if is_internal_job(str(job_type or "")):
                continue
            # SQLite raw SQL 返回 JSON 文本；解析为结构化对象后再精确匹配。
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, ValueError):
                    payload = None
            refs = job_reference_ids(str(job_type or ""), payload)
            op_tokens = operation_id_tokens(str(job_type or ""), str(operation_id or ""))
            job_trace = str(trace_id) if trace_id else ""
            created = _as_utc(created_at)
            in_cutoff = created is not None and cutoff is not None and created <= cutoff

            # 1) 强关联：精确来源引用 → 即使 Job 在 cutoff 后创建也必须清理。
            #    operation_id 只做「整段 token 精确相等」，不做子串包含。
            strong = bool(
                (refs.event_ids & target_events)
                or (refs.turn_ids & target_turns)
                or (refs.memory_ids & target_memories)
                or (refs.child_ids & target_children)
                or (refs.trace_ids & target_traces)
                or (job_trace and job_trace in target_traces)
                or (
                    op_tokens
                    & (
                        target_events | target_turns | target_memories
                        | target_children | target_traces
                    )
                )
            )
            if not strong and job_type == "retrieval_index_refresh" and isinstance(
                payload, dict,
            ):
                bucket = source_sets.get(str(payload.get("source_type") or ""))
                source_id = str(payload.get("source_id") or "")
                if bucket is not None and source_id and source_id in bucket:
                    strong = True
            if strong:
                matched.add(job_id)
                if refs.unparseable:
                    unparseable.add(job_id)
                continue

            # 2) cutoff 之后独立创建、且只引用 Thread/父 Task 的新 Job 保留。
            if not in_cutoff:
                continue

            # 3) cutoff 之前的弱关联：按 Selector 范围 + Job 创建时间判定。
            weak = False
            if scope.all_user:
                weak = True
            elif scope.selector_type == "thread" and (refs.thread_ids & target_threads):
                weak = True
            elif scope.selector_type == "time_range":
                if tf is not None and created is not None and created < tf:
                    weak = False
                elif tt is not None and created is not None and created > tt:
                    weak = False
                else:
                    weak = True
            elif refs.task_ids & target_full_tasks:
                weak = True
            elif op_tokens & target_full_tasks:
                weak = True
            elif (
                scope.selector_type == "thread"
                and (op_tokens & target_threads)
            ):
                weak = True
            if weak:
                matched.add(job_id)
                if refs.unparseable:
                    unparseable.add(job_id)
    return matched, unparseable


def build_scope(
    db: Session, operation_id: str, mode: str, selector_payload: dict[str, Any],
) -> ContentScope:
    """把 selector_payload 解析为实际清理范围。"""
    selector_type = str(selector_payload.get("selector_type") or "")
    scope = ContentScope(
        operation_id=operation_id,
        mode=mode,
        selector_type=selector_type,
        cutoff=parse_dt(selector_payload.get("cutoff_created_at")) or datetime.now().astimezone(),
        time_from=parse_dt(selector_payload.get("time_from")),
        time_to=parse_dt(selector_payload.get("time_to")),
        all_user=bool(selector_payload.get("all_user_data")),
    )

    # 目标 memory（宽选择器由 Cascade 物化，窄选择器由 Phase A 写入）
    scope.memory_ids = _ids_from_query(
        db, db.query(ForgetTarget.target_id).filter_by(
            forget_operation_id=operation_id, target_type="memory_record",
        )
    )
    if scope.all_user:
        # all_user_data 的兜底：只纳入 Phase A cutoff 之前已存在的 memory；
        # 请求之后新产生的记忆必须保留。
        scope.memory_ids |= _ids_from_query(
            db, db.query(MemoryRecord.id).filter(
                MemoryRecord.created_at <= scope.cutoff,
            )
        )
    explicit_events = _ids_from_query(
        db, db.query(ForgetTarget.target_id).filter_by(
            forget_operation_id=operation_id, target_type="event",
        )
    )
    explicit_turns = _ids_from_query(
        db, db.query(ForgetTarget.target_id).filter_by(
            forget_operation_id=operation_id, target_type="turn_record",
        )
    )

    tid = selector_payload.get("thread_id")
    if tid:
        scope.thread_ids = {str(tid)}

    # ── Turn token 展开 ──
    tokens = normalize_turn_tokens(db, sorted(explicit_turns))
    def _add_turn_rows(rows: list[Any]) -> None:
        for row in rows:
            if row[0]:
                tokens.add(str(row[0]))
            if row[1]:
                tokens.add(str(row[1]))

    if scope.thread_ids:
        _add_turn_rows(db.query(TurnRecord.id, TurnRecord.turn_id).filter(
            TurnRecord.thread_id.in_(scope.thread_ids),
            TurnRecord.created_at <= scope.cutoff,
        ).all())
    if scope.time_from or scope.time_to:
        tq = db.query(TurnRecord.id, TurnRecord.turn_id).filter(
            TurnRecord.created_at <= scope.cutoff,
        )
        for cond in _time_filter(TurnRecord.created_at, scope.time_from, scope.time_to):
            tq = tq.filter(cond)
        _add_turn_rows(tq.all())
    if scope.all_user:
        _add_turn_rows(db.query(TurnRecord.id, TurnRecord.turn_id).filter(
            TurnRecord.created_at <= scope.cutoff,
        ).all())
    scope.turn_tokens = tokens
    scope.raw_turn_tokens = set(tokens)

    # ── Event 范围 ──
    # 显式 Turn Selector 与间接发现的 Turn 都要按 cutoff 冻结；间接 Turn 在
    # time_range 下还须逐个 Event 满足时间窗口，不能因父 Turn 命中扩大范围。
    explicit_turn_selector = selector_type == "turn_ids"
    events: set[str] = set(explicit_events)
    if tokens:
        child_eq = db.query(Event.id).filter(
            Event.turn_id.in_(tokens),
            Event.created_at <= scope.cutoff,
        )
        if not explicit_turn_selector and (scope.time_from or scope.time_to):
            for cond in _time_filter(
                Event.created_at, scope.time_from, scope.time_to,
            ):
                child_eq = child_eq.filter(cond)
        events |= _ids_from_query(db, child_eq)
    if scope.thread_ids:
        events |= _ids_from_query(
            db, db.query(Event.id).filter(
                Event.thread_id.in_(scope.thread_ids),
                Event.created_at <= scope.cutoff,
            )
        )
    if scope.time_from or scope.time_to:
        eq = db.query(Event.id).filter(Event.created_at <= scope.cutoff)
        for cond in _time_filter(Event.created_at, scope.time_from, scope.time_to):
            eq = eq.filter(cond)
        events |= _ids_from_query(db, eq)
    if scope.all_user:
        events |= _ids_from_query(
            db, db.query(Event.id).filter(Event.created_at <= scope.cutoff)
        )
    scope.raw_event_ids = set(events)
    if scope.memory_ids:
        events |= _ids_from_query(
            db, db.query(ContentProvenanceRef.source_id).filter(
                ContentProvenanceRef.owner_type == "memory_record",
                ContentProvenanceRef.source_type == "event",
                ContentProvenanceRef.owner_id.in_(scope.memory_ids),
            )
        )
        events |= _ids_from_query(
            db, db.query(MemoryEvidence.source_event_id).filter(
                MemoryEvidence.memory_id.in_(scope.memory_ids),
                MemoryEvidence.source_event_id.isnot(None),
            )
        )
        events |= _ids_from_query(
            db, db.query(MemoryRecord.source_event_id).filter(
                MemoryRecord.id.in_(scope.memory_ids),
                MemoryRecord.source_event_id.isnot(None),
            )
        )
    scope.event_ids = events

    # event 范围补全 turn token（用于 proposals / summaries 匹配）
    if events:
        scope.turn_tokens |= _ids_from_query(
            db, db.query(Event.turn_id).filter(
                Event.id.in_(events), Event.turn_id.isnot(None),
            )
        )
        scope.trace_ids = _ids_from_query(
            db, db.query(Event.trace_id).filter(Event.id.in_(events))
        )

    # ── Segment / Epoch ──
    segments: set[str] = set()
    if scope.thread_ids:
        segments |= _ids_from_query(
            db, db.query(Segment.id).filter(
                Segment.thread_id.in_(scope.thread_ids),
                Segment.created_at <= scope.cutoff,
            )
        )
    if scope.time_from or scope.time_to:
        sq = db.query(Segment.id).filter(Segment.created_at <= scope.cutoff)
        for cond in _time_filter(Segment.created_at, scope.time_from, scope.time_to):
            sq = sq.filter(cond)
        segments |= _ids_from_query(db, sq)
    if scope.all_user:
        segments |= _ids_from_query(
            db, db.query(Segment.id).filter(Segment.created_at <= scope.cutoff)
        )
    if scope.turn_tokens:
        segments |= _ids_from_query(
            db, db.query(TurnRecord.segment_id).filter(
                TurnRecord.id.in_(scope.turn_tokens),
                TurnRecord.segment_id.isnot(None),
            )
        )
    if events:
        segments |= {
            str(sid) for sid in select_json_array_members(
                db, "segment_summaries", "segment_id", "source_event_ids", sorted(events),
            ) if sid
        }
    if scope.turn_tokens:
        segments |= {
            str(sid) for sid in select_json_array_members(
                db, "segment_summaries", "segment_id", "source_turn_ids",
                sorted(scope.turn_tokens),
            ) if sid
        }
    scope.segment_ids = segments

    epochs: set[str] = set()
    if segments:
        epochs |= _ids_from_query(
            db, db.query(Segment.epoch_id).filter(
                Segment.id.in_(segments), Segment.epoch_id.isnot(None),
            )
        )
    if scope.thread_ids:
        epochs |= _ids_from_query(
            db, db.query(Epoch.id).filter(
                Epoch.thread_id.in_(scope.thread_ids),
                Epoch.created_at <= scope.cutoff,
            )
        )
    if scope.time_from or scope.time_to:
        epq = db.query(Epoch.id).filter(Epoch.created_at <= scope.cutoff)
        for cond in _time_filter(Epoch.created_at, scope.time_from, scope.time_to):
            epq = epq.filter(cond)
        epochs |= _ids_from_query(db, epq)
    if scope.all_user:
        epochs |= _ids_from_query(
            db, db.query(Epoch.id).filter(Epoch.created_at <= scope.cutoff)
        )
    scope.epoch_ids = epochs

    # ── 派生摘要 / 检查点 ID（供 Outbox operation_id 精确关联）──
    summary_ids: set[str] = set()
    if segments:
        summary_ids |= _ids_from_query(
            db, db.query(SegmentSummary.id).filter(
                SegmentSummary.segment_id.in_(segments)
            )
        )
    if events:
        summary_ids |= set(select_json_array_members(
            db, "segment_summaries", "id", "source_event_ids", sorted(events),
        ))
    if scope.turn_tokens:
        summary_ids |= set(select_json_array_members(
            db, "segment_summaries", "id", "source_turn_ids",
            sorted(scope.turn_tokens),
        ))
    scope.summary_ids = summary_ids

    checkpoint_ids: set[str] = set()
    if epochs:
        checkpoint_ids |= _ids_from_query(
            db, db.query(EpochCheckpoint.id).filter(
                EpochCheckpoint.epoch_id.in_(epochs)
            )
        )
    if segments:
        checkpoint_ids |= set(select_json_array_members(
            db, "epoch_checkpoints", "id", "source_segment_ids", sorted(segments),
        ))
    scope.checkpoint_ids = checkpoint_ids

    # ── Task Runtime ──
    tasks: set[str] = set()
    if scope.thread_ids:
        tasks |= _ids_from_query(
            db, db.query(AgentTask.id).filter(
                AgentTask.thread_id.in_(scope.thread_ids),
                AgentTask.created_at <= scope.cutoff,
            )
        )
    if scope.time_from or scope.time_to:
        tq = db.query(AgentTask.id).filter(AgentTask.created_at <= scope.cutoff)
        for cond in _time_filter(AgentTask.created_at, scope.time_from, scope.time_to):
            tq = tq.filter(cond)
        tasks |= _ids_from_query(db, tq)
    if scope.all_user:
        tasks |= _ids_from_query(
            db, db.query(AgentTask.id).filter(AgentTask.created_at <= scope.cutoff)
        )
    if scope.turn_tokens:
        tasks |= _ids_from_query(
            db, db.query(AgentTask.id).filter(
                AgentTask.source_turn_record_id.in_(scope.turn_tokens)
            )
        )
    scope.agent_task_ids = tasks

    # 子记录集合独立维护：每条子记录按自身 created_at + cutoff/time_range 命中，
    # 不因为父 Task 命中就扩大到全部子记录。父 Task 未命中但子记录命中时，
    # 父 Task 只做局部状态脱敏，不整体清空。
    time_selector = scope.time_from is not None or scope.time_to is not None
    child_parent_ids: set[str] = set()
    for model, attr in (
        (AgentRun, "agent_run_ids"),
        (AgentAction, "agent_action_ids"),
        (AgentTaskEvent, "agent_task_event_ids"),
        (AgentTaskCheckpoint, "agent_task_checkpoint_ids"),
        (TaskEvidence, "task_evidence_ids"),
        (TaskArtifact, "task_artifact_ids"),
    ):
        q = db.query(model.id, model.task_id).filter(
            model.created_at <= scope.cutoff,
        )
        if time_selector:
            for cond in _time_filter(
                model.created_at, scope.time_from, scope.time_to,
            ):
                q = q.filter(cond)
        elif tasks:
            q = q.filter(model.task_id.in_(tasks))
        else:
            setattr(scope, attr, set())
            continue
        rows = q.all()
        setattr(scope, attr, {str(row[0]) for row in rows if row[0]})
        child_parent_ids |= {str(row[1]) for row in rows if row[1]}
    scope.partial_agent_task_ids = child_parent_ids - scope.agent_task_ids

    # ── Knowledge（仅 all_user_data，或 everywhere + time_range）──
    documents: set[str] = set()
    if scope.all_user:
        documents |= _ids_from_query(
            db, db.query(Document.id).filter(Document.created_at <= scope.cutoff)
        )
    elif scope.time_from or scope.time_to:
        if mode == "everywhere":
            dq = db.query(Document.id).filter(Document.created_at <= scope.cutoff)
            for cond in _time_filter(Document.created_at, scope.time_from, scope.time_to):
                dq = dq.filter(cond)
            documents |= _ids_from_query(db, dq)
    scope.document_ids = documents

    # ── MemoryConflictCase ──
    cases: set[str] = set()
    if scope.memory_ids:
        cases |= _ids_from_query(
            db, db.query(ConflictCase.id).filter(
                (ConflictCase.old_memory_id.in_(scope.memory_ids))
                | (ConflictCase.resolved_memory_id.in_(scope.memory_ids))
            )
        )
    if events:
        cases |= _ids_from_query(
            db, db.query(ConflictCase.id).filter(
                ConflictCase.question_event_id.in_(events)
            )
        )
    if scope.thread_ids:
        cases |= _ids_from_query(
            db, db.query(ConflictCase.id).filter(
                ConflictCase.thread_id.in_(scope.thread_ids),
                ConflictCase.created_at <= scope.cutoff,
            )
        )
    if scope.time_from or scope.time_to:
        cq = db.query(ConflictCase.id).filter(ConflictCase.created_at <= scope.cutoff)
        for cond in _time_filter(ConflictCase.created_at, scope.time_from, scope.time_to):
            cq = cq.filter(cond)
        cases |= _ids_from_query(db, cq)
    if scope.all_user:
        cases |= _ids_from_query(
            db, db.query(ConflictCase.id).filter(ConflictCase.created_at <= scope.cutoff)
        )
    if events or scope.turn_tokens:
        proposal_ids: set[str] = set()
        if events:
            proposal_ids |= set(select_json_array_members(
                db, "memory_proposals", "id", "source_event_ids", sorted(events),
            ))
        if scope.turn_tokens:
            proposal_ids |= _ids_from_query(
                db, db.query(MemoryProposal.id).filter(
                    (MemoryProposal.source_turn_id.in_(scope.turn_tokens))
                    | (MemoryProposal.source_turn_record_id.in_(scope.turn_tokens))
                )
            )
        if proposal_ids:
            cases |= _ids_from_query(
                db, db.query(ConflictCase.id).filter(
                    ConflictCase.proposal_id.in_(proposal_ids)
                )
            )
    scope.conflict_case_ids = cases

    # ── ToolOperation：按真实 Thread/Turn 关联发现（与 content_scrub 同一判定）──
    from aiive.forget.content_scrub import tool_operation_ids as _tool_ops

    scope.tool_operation_ids = _tool_ops(db, scope)

    # ── MemoryRecallRun / RetrievalRun ──
    recall_runs: set[str] = set()
    if scope.trace_ids:
        recall_runs |= _ids_from_query(
            db, db.query(MemoryRecallRun.id).filter(
                MemoryRecallRun.trace_id.in_(scope.trace_ids)
            )
        )
    if scope.memory_ids:
        recall_runs |= _ids_from_query(
            db, db.query(MemoryRecallCandidate.run_id).filter(
                MemoryRecallCandidate.memory_id.in_(scope.memory_ids)
            )
        )
    if scope.time_from or scope.time_to:
        rq = db.query(MemoryRecallRun.id).filter(MemoryRecallRun.created_at <= scope.cutoff)
        for cond in _time_filter(MemoryRecallRun.created_at, scope.time_from, scope.time_to):
            rq = rq.filter(cond)
        recall_runs |= _ids_from_query(db, rq)
    if scope.all_user:
        recall_runs |= _ids_from_query(
            db, db.query(MemoryRecallRun.id).filter(
                MemoryRecallRun.created_at <= scope.cutoff,
            )
        )
    scope.recall_run_ids = recall_runs

    retrieval_runs: set[str] = set()
    if scope.trace_ids:
        retrieval_runs |= _ids_from_query(
            db, db.query(RetrievalRun.id).filter(RetrievalRun.trace_id.in_(scope.trace_ids))
        )
    if scope.time_from or scope.time_to:
        rrq = db.query(RetrievalRun.id).filter(RetrievalRun.created_at <= scope.cutoff)
        for cond in _time_filter(RetrievalRun.created_at, scope.time_from, scope.time_to):
            rrq = rrq.filter(cond)
        retrieval_runs |= _ids_from_query(db, rrq)
    if scope.all_user:
        retrieval_runs |= _ids_from_query(
            db, db.query(RetrievalRun.id).filter(RetrievalRun.created_at <= scope.cutoff)
        )
    scope.retrieval_run_ids = retrieval_runs

    # ── Outbox：精确结构化引用 + 有界分批发现 ──
    scope.outbox_job_ids, scope.outbox_unparseable_ids = _discover_outbox_jobs(
        db, scope,
    )

    # 目标文档的分块（用于 Verify 计数）

    scope.segment_ids = {s for s in scope.segment_ids if s}
    return scope


def load_scope(
    db: Session, operation_id: str, mode: str,
) -> ContentScope | None:
    manifest = (
        db.query(ForgetSelectorManifest)
        .filter_by(forget_operation_id=operation_id)
        .first()
    )
    if manifest is None:
        return None
    payload = dict(manifest.selector_payload or {})
    payload.setdefault("cutoff_created_at", manifest.cutoff_created_at)
    return build_scope(db, operation_id, mode, payload)


def document_chunk_ids(db: Session, document_ids: set[str]) -> list[str]:
    if not document_ids:
        return []
    return [
        str(r[0]) for r in db.query(Chunk.id).filter(
            Chunk.document_id.in_(document_ids)
        ).all()
    ]


def count_rows(db: Session, table: str, where: str, params: dict[str, Any] | None = None) -> int:
    return int(db.execute(
        sa_text(f"SELECT COUNT(*) FROM {table} WHERE {where}"), params or {},
    ).scalar() or 0)
