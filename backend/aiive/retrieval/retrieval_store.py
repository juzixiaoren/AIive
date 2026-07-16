"""Phase 5：源 ORM → 检索 Entry 投影构造（每个 source_type 一个 builder）。

只产出 upsert_entry 所需的字段与 token 列表；生命周期快照由源记录真实字段填入，
不引入 embedding。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import Epoch, EpochCheckpoint, Segment, SegmentSummary
from aiive.retrieval.index_tokenizer import tokenize_for_index
from aiive.retrieval.source_version import (
    epoch_checkpoint_source_version,
    memory_source_version,
    segment_summary_source_version,
)

logger = logging.getLogger(__name__)


def _build_memory(r: Any) -> dict[str, Any]:
    version, h = memory_source_version(r)
    search_text = f"{r.canonical_key or ''} {r.content or ''}"
    tier = "warm" if r.lifecycle_state == "active" else "cold"
    return {
        "source_type": "memory_record",
        "source_id": r.id,
        "source_version": version,
        "source_hash": h,
        "title": r.canonical_key or (r.content or "")[:40],
        "search_text": search_text,
        "snippet": (r.content or "")[:200],
        "scope_type": r.scope_type or "global",
        "scope_id": r.scope_id,
        "canonical_key": r.canonical_key,
        "lifecycle_state": r.lifecycle_state,
        "validity_state": r.validity_state,
        "retrieval_tier": tier,
        "thread_id": None,
        "epoch_id": None,
        "segment_id": None,
        "memory_record_id": r.id,
        "metadata": {"memory_type": r.memory_type, "pinned": bool(r.pinned)},
        "created_source_at": r.created_at,
        "updated_source_at": r.updated_at,
        "tokens": tokenize_for_index(search_text),
        "is_forgotten": r.lifecycle_state == "forgotten",
    }


def _build_summary(db: Session, s: SegmentSummary) -> dict[str, Any]:
    version, h = segment_summary_source_version(s)
    decisions = s.decisions or []
    decisions_text = " ".join(
        str(d.get("what") if isinstance(d, dict) else d) for d in decisions
    )
    search_text = f"{s.goal or ''} {s.outcome or ''} {decisions_text}"
    thread_id = None
    epoch_id = None
    seg = db.query(Segment).filter(Segment.id == s.segment_id).first()
    if seg is not None:
        epoch_id = seg.epoch_id
        epoch = db.query(Epoch).filter(Epoch.id == seg.epoch_id).first()
        if epoch is not None:
            thread_id = epoch.thread_id
    return {
        "source_type": "segment_summary",
        "source_id": s.id,
        "source_version": version,
        "source_hash": h,
        "title": s.goal or "Segment Summary",
        "search_text": search_text,
        "snippet": (s.outcome or "")[:200],
        "scope_type": "global",
        "scope_id": None,
        "canonical_key": None,
        "lifecycle_state": "valid",
        "validity_state": "valid",
        "retrieval_tier": "warm",
        "thread_id": thread_id,
        "epoch_id": epoch_id,
        "segment_id": s.segment_id,
        "memory_record_id": None,
        "metadata": {"summary_version": s.summary_version},
        "created_source_at": s.created_at,
        "updated_source_at": s.created_at,
        "tokens": tokenize_for_index(search_text),
        "is_forgotten": False,
    }


def _build_checkpoint(c: EpochCheckpoint) -> dict[str, Any]:
    version, h = epoch_checkpoint_source_version(c)
    constraints = c.active_constraints or []
    constraints_text = " ".join(
        str(x.get("description") if isinstance(x, dict) else x) for x in constraints
    )
    loops = c.open_loops or []
    loops_text = " ".join(
        str(x.get("description") if isinstance(x, dict) else x) for x in loops
    )
    search_text = f"{c.current_goal or ''} {loops_text} {constraints_text}"
    return {
        "source_type": "epoch_checkpoint",
        "source_id": c.id,
        "source_version": version,
        "source_hash": h,
        "title": c.current_goal or "Epoch Checkpoint",
        "search_text": search_text,
        "snippet": (c.current_goal or "")[:200],
        "scope_type": "global",
        "scope_id": None,
        "canonical_key": None,
        "lifecycle_state": "valid",
        "validity_state": "valid",
        "retrieval_tier": "warm",
        "thread_id": None,
        "epoch_id": c.epoch_id,
        "segment_id": None,
        "memory_record_id": None,
        "metadata": {"checkpoint_version": c.version},
        "created_source_at": c.created_at,
        "updated_source_at": c.created_at,
        "tokens": tokenize_for_index(search_text),
        "is_forgotten": False,
    }


def build_entry_fields(
    db: Session, source_type: str, orm: Any,
) -> dict[str, Any] | None:
    """根据 source_type 构造 Entry 投影字段（含 tokens）。

    返回 None 表示该源不可索引（如类型未知）。
    """
    if source_type == "memory_record":
        return _build_memory(orm)
    if source_type == "segment_summary":
        return _build_summary(db, orm)
    if source_type == "epoch_checkpoint":
        return _build_checkpoint(orm)
    logger.warning("未知 source_type，跳过索引: %s", source_type)
    return None
