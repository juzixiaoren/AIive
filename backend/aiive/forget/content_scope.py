# pyright: reportImplicitStringConcatenation=false
"""Forget 宽范围内容范围解析。

把 SelectorManifest 解析成一组目标 ID / 时间窗口，供 Scrub 与 Verify 共用。
原则：
  - 不按表名猜测关系，所有关联都基于 ORM 中真实存在的列/JSON 引用；
  - 不在 SQL 中用 LIKE 判断 JSON 数组成员；
  - memory_only 保留 Event/Turn 原始正文，但派生内容仍纳入范围。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam, text as sa_text
from sqlalchemy.orm import Session

from aiive.db.forget_models import (
    ForgetSelectorManifest,
    ForgetTarget,
)
from aiive.db.models import (
    AgentTask,
    Chunk,
    Document,
    Epoch,
    Event,
    MemoryEvidence,
    MemoryRecord,
    Segment,
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
        # all_user_data 的兜底：即使 Cascade 物化中断，也不漏任何 memory。
        scope.memory_ids |= _ids_from_query(db, db.query(MemoryRecord.id))
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
        ).all())
    if scope.time_from or scope.time_to:
        tq = db.query(TurnRecord.id, TurnRecord.turn_id)
        for cond in _time_filter(TurnRecord.created_at, scope.time_from, scope.time_to):
            tq = tq.filter(cond)
        _add_turn_rows(tq.all())
    if scope.all_user:
        _add_turn_rows(db.query(TurnRecord.id, TurnRecord.turn_id).all())
    scope.turn_tokens = tokens
    scope.raw_turn_tokens = set(tokens)

    # ── Event 范围 ──
    events: set[str] = set(explicit_events)
    if tokens:
        events |= _ids_from_query(
            db, db.query(Event.id).filter(Event.turn_id.in_(tokens))
        )
    if scope.thread_ids:
        events |= _ids_from_query(
            db, db.query(Event.id).filter(Event.thread_id.in_(scope.thread_ids))
        )
    if scope.time_from or scope.time_to:
        eq = db.query(Event.id)
        for cond in _time_filter(Event.created_at, scope.time_from, scope.time_to):
            eq = eq.filter(cond)
        events |= _ids_from_query(db, eq)
    if scope.all_user:
        events |= _ids_from_query(db, db.query(Event.id))
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

    # ── Segment / Epoch ──
    segments: set[str] = set()
    if scope.thread_ids:
        segments |= _ids_from_query(
            db, db.query(Segment.id).filter(Segment.thread_id.in_(scope.thread_ids))
        )
    if scope.time_from or scope.time_to:
        sq = db.query(Segment.id)
        for cond in _time_filter(Segment.created_at, scope.time_from, scope.time_to):
            sq = sq.filter(cond)
        segments |= _ids_from_query(db, sq)
    if scope.all_user:
        segments |= _ids_from_query(db, db.query(Segment.id))
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
            db, db.query(Epoch.id).filter(Epoch.thread_id.in_(scope.thread_ids))
        )
    if scope.time_from or scope.time_to:
        epq = db.query(Epoch.id)
        for cond in _time_filter(Epoch.created_at, scope.time_from, scope.time_to):
            epq = epq.filter(cond)
        epochs |= _ids_from_query(db, epq)
    if scope.all_user:
        epochs |= _ids_from_query(db, db.query(Epoch.id))
    scope.epoch_ids = epochs

    # ── Task Runtime ──
    tasks: set[str] = set()
    if scope.thread_ids:
        tasks |= _ids_from_query(
            db, db.query(AgentTask.id).filter(AgentTask.thread_id.in_(scope.thread_ids))
        )
    if scope.time_from or scope.time_to:
        tq = db.query(AgentTask.id)
        for cond in _time_filter(AgentTask.created_at, scope.time_from, scope.time_to):
            tq = tq.filter(cond)
        tasks |= _ids_from_query(db, tq)
    if scope.all_user:
        tasks |= _ids_from_query(db, db.query(AgentTask.id))
    if scope.turn_tokens:
        tasks |= _ids_from_query(
            db, db.query(AgentTask.id).filter(
                AgentTask.source_turn_record_id.in_(scope.turn_tokens)
            )
        )
    scope.agent_task_ids = tasks

    # ── Knowledge（仅 all_user_data，或 everywhere + time_range）──
    documents: set[str] = set()
    if scope.all_user:
        documents |= _ids_from_query(db, db.query(Document.id))
    elif scope.time_from or scope.time_to:
        if mode == "everywhere":
            dq = db.query(Document.id)
            for cond in _time_filter(Document.created_at, scope.time_from, scope.time_to):
                dq = dq.filter(cond)
            documents |= _ids_from_query(db, dq)
    scope.document_ids = documents

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
