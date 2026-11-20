"""
API路由模块：诊断检查器
- 提供上下文构建运行详情和检索运行详情查询
- 提供事件诊断检查接口
"""
import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.core.assistant_protocol import public_event_payload
from aiive.db.base import get_db
from aiive.db.models import (
    ContextSnapshot,
    Event,
    RetrievalRun,
    RetrievalCandidate,
    MemoryRecallRun,
    MemoryRecallCandidate,
)

from aiive.forget.visibility_service import ForgetVisibilityService
from aiive.runtime.context_preview import context_content_preview

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


class ContextItemPreview(BaseModel):
    item_id: str
    kind: str
    source: str
    trust_level: str
    content_preview: str
    preview_length: int
    preview_truncated: bool
    has_detail: bool
    token_estimate: int


class InjectedMemory(BaseModel):
    memory_id: str
    canonical_key: str | None = None
    memory_type: str | None = None


class ContextSelectionCandidate(BaseModel):
    candidate_id: str
    source_type: str
    source_id: str
    source_version: str | None = None
    retention_tier: int
    native_score: float | None = None
    source_rank: int
    rank_percentile: float
    estimated_tokens: int | None = None
    selected: bool
    exclusion_reason: str | None = None


class ContextSelectionTrace(BaseModel):
    policy_version: str | None = None
    candidates: list[ContextSelectionCandidate] = Field(default_factory=list)
    eviction_order: list[str] = Field(default_factory=list)


class ContextSnapshotSummary(BaseModel):
    snapshot_id: str
    trace_id: str
    stable_prefix_hash: str
    context_items: list[ContextItemPreview]
    token_total: int
    total_tool_calls: int
    created_at: datetime
    selection_trace: ContextSelectionTrace
    injected_memory_ids: list[InjectedMemory]


class ContextRunResponse(BaseModel):
    trace_id: str
    snapshots: list[ContextSnapshotSummary]


class ContextItemDetailResponse(BaseModel):
    snapshot_id: str
    item_id: str
    full_content: str
    content_length: int


def _snapshot_not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "context_snapshot_not_found", "message": "未找到上下文快照"},
    )


def _disable_context_http_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _snapshot_summary(snapshot: ContextSnapshot) -> dict[str, Any]:
    meta = snapshot.meta or {}
    full_contents = meta.get("full_contents") or {}
    items: list[dict[str, Any]] = []
    for item in snapshot.context_items or []:
        item_id = item.get("item_id")
        if not isinstance(item_id, str):
            continue
        stored_content = full_contents.get(item_id)
        has_detail = isinstance(stored_content, str)
        # Rebuild old 20/120-character previews. Legacy goal traces have inline text only.
        text = stored_content if has_detail else item.get("content", item.get("content_preview", ""))
        text = text if isinstance(text, str) else ""
        preview = context_content_preview(text)
        length = len(text) if has_detail or "content" in item else max(len(text), item.get("preview_length") or 0)
        items.append({
            "item_id": item_id,
            "kind": item.get("kind") or item.get("role") or "unknown",
            "source": item.get("source") or item.get("role") or "unknown",
            "trust_level": item.get("trust_level") or "unknown",
            "content_preview": preview,
            "preview_length": length,
            "preview_truncated": len(preview) < length,
            "has_detail": has_detail,
            "token_estimate": item.get("token_estimate") or 0,
        })
    return {
        "snapshot_id": snapshot.id,
        "trace_id": snapshot.trace_id,
        "stable_prefix_hash": snapshot.stable_prefix_hash,
        "context_items": items,
        "token_total": snapshot.token_total,
        "total_tool_calls": meta.get("total_tool_calls", 0),
        "created_at": snapshot.created_at,
        "selection_trace": meta.get("selection_trace") or {},
        "injected_memory_ids": meta.get("injected_memory_ids") or [],
    }


@router.get(
    "/context-runs/{trace_id}", response_model=ContextRunResponse,
    dependencies=[Depends(_disable_context_http_cache)],
)
def get_context_run(trace_id: str, db: Session = Depends(get_db)):
    """获取指定 trace 的可见快照摘要。"""
    snapshots = (
        db.query(ContextSnapshot)
        .filter(ContextSnapshot.trace_id == trace_id)
        .order_by(ContextSnapshot.created_at.desc(), ContextSnapshot.id.desc())
        .limit(10)
        .all()
    )
    blocked = ForgetVisibilityService.blocked_context_snapshot_ids(db, snapshots)
    summaries = [_snapshot_summary(snapshot) for snapshot in snapshots if snapshot.id not in blocked]
    if not summaries:
        raise _snapshot_not_found()
    return {"trace_id": trace_id, "snapshots": summaries}


@router.get(
    "/context-runs/{trace_id}/items/{item_id}", response_model=ContextItemDetailResponse,
    dependencies=[Depends(_disable_context_http_cache)],
)
def get_context_item_detail(
    trace_id: str, item_id: str, snapshot_id: Annotated[str, Query(min_length=1)],
    db: Session = Depends(get_db),
):
    """读取指定快照内单个可见上下文项的完整内容。"""
    snapshot = db.query(ContextSnapshot).filter(
        ContextSnapshot.id == snapshot_id, ContextSnapshot.trace_id == trace_id,
    ).one_or_none()
    if snapshot is None or ForgetVisibilityService.blocked_context_snapshot_ids(db, [snapshot]):
        raise _snapshot_not_found()

    content = None
    if any(item.get("item_id") == item_id for item in snapshot.context_items or []):
        content = ((snapshot.meta or {}).get("full_contents") or {}).get(item_id)
    if not isinstance(content, str):
        raise HTTPException(
            status_code=404,
            detail={"code": "context_item_not_found", "message": "未找到上下文项"},
        )
    return {
        "snapshot_id": snapshot.id, "item_id": item_id,
        "full_content": content, "content_length": len(content),
    }


@router.get("/retrieval-runs")
def list_retrieval_runs(
    trace_id: str = Query(...),
    db: Session = Depends(get_db),
):
    """按 trace_id 获取本轮真实检索运行摘要。"""
    runs = (
        db.query(RetrievalRun)
        .filter(RetrievalRun.trace_id == trace_id)
        .order_by(RetrievalRun.created_at.desc())
        .all()
    )
    return [{
        "run_id": run.id,
        "trace_id": run.trace_id,
        "query": run.query,
        "strategy": run.strategy,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    } for run in runs]


@router.get("/retrieval-runs/{run_id}")
def get_retrieval_run(run_id: str, db: Session = Depends(get_db)):
    """获取指定 run_id 的检索运行详情

    Args:
        run_id: 检索运行ID
        db: 数据库会话

    Returns:
        检索运行详情，包含查询语句、策略和候选结果列表
    """
    try:
        run = db.get(RetrievalRun, run_id)
        if not run:
            raise HTTPException(
                status_code=404,
                detail={"code": "retrieval_run_not_found", "message": "未找到检索运行记录"},
            )

        candidates = (
            db.query(RetrievalCandidate)
            .filter(RetrievalCandidate.run_id == run_id)
            .all()
        )
        return {
            "run_id": run.id,
            "query": run.query,
            "strategy": run.strategy,
            "candidates": [
                {"source_id": c.source_id, "source_type": c.source_type, "score": c.score}
                for c in candidates
            ],
        }
    except Exception:
        logger.exception("获取检索运行详情失败: run_id=%s", run_id)
        raise


@router.get("/memory-recall-runs")
def list_memory_recall_runs(
    trace_id: str = Query(...),
    db: Session = Depends(get_db),
):
    """按 trace_id 获取本轮真实记忆召回运行摘要。"""
    runs = (
        db.query(MemoryRecallRun)
        .filter(MemoryRecallRun.trace_id == trace_id)
        .order_by(MemoryRecallRun.created_at.desc())
        .all()
    )
    return [{
        "run_id": run.id,
        "trace_id": run.trace_id,
        "request_query": run.request_query,
        "result_count": run.result_count,
        "total_latency_ms": run.total_latency_ms,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    } for run in runs]


@router.get("/memory-recall-runs/{run_id}")
def get_memory_recall_run(run_id: str, db: Session = Depends(get_db)):
    """获取指定 run_id 的 V2 记忆召回运行详情（含每个候选的路由、原始分、融合分、入选/排除原因）。

    Args:
        run_id: 记忆召回运行ID
        db: 数据库会话

    Returns:
        召回运行详情 + 候选列表（供 Retrieval Inspector 解释召回过程）
    """
    try:
        run = db.get(MemoryRecallRun, run_id)
        if not run:
            # 与 get_retrieval_run / get_context_run 保持一致的 404 语义，
            # 200 + {"error": ...} 会被前端当作正常详情解析导致崩溃
            raise HTTPException(
                status_code=404,
                detail={"code": "memory_recall_run_not_found", "run_id": run_id},
            )
        candidates = (
            db.query(MemoryRecallCandidate)
            .filter(MemoryRecallCandidate.run_id == run_id)
            .all()
        )
        return {
            "run_id": run.id,
            "trace_id": run.trace_id,
            "request_query": run.request_query,
            "scope_context": run.scope_context,
            "routes_executed": run.routes_executed,
            "token_budget": run.token_budget,
            "result_count": run.result_count,
            "total_latency_ms": run.total_latency_ms,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "candidates": [
                {
                    "memory_id": c.memory_id,
                    "route": c.route,
                    "raw_score": c.raw_score,
                    "fused_score": c.fused_score,
                    "selected": c.selected,
                    "exclusion_reason": c.exclusion_reason,
                    "token_cost": c.token_cost,
                }
                for c in candidates
            ],
        }
    except Exception:
        logger.exception("获取记忆召回运行详情失败: run_id=%s", run_id)
        raise


@router.get("/inspector/events")
def inspector_events(
    thread_id: str | None = Query(None),
    trace_id: str | None = Query(None),
    limit: int = Query(50),
    db: Session = Depends(get_db),
):
    """诊断事件查询接口，支持按 thread_id 或 trace_id 过滤

    Args:
        thread_id: 会话ID（可选）
        trace_id: 追踪ID（可选）
        limit: 返回数量限制，默认50
        db: 数据库会话

    Returns:
        事件列表，按创建时间升序排列
    """
    try:
        q = db.query(Event)
        if thread_id:
            q = q.filter(Event.thread_id == thread_id)
        if trace_id:
            q = q.filter(Event.trace_id == trace_id)
        # 取最近的 limit 条再反转为升序展示——若直接升序取前 N 条，
        # 系统运行一段时间后该接口永远只返回最旧的一批事件
        events = list(reversed(q.order_by(Event.created_at.desc()).limit(limit).all()))
        return [
            {
                "id": e.id,
                "trace_id": e.trace_id,
                "event_type": e.event_type,
                "payload": public_event_payload(e.payload),
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ]
    except Exception:
        logger.exception("诊断事件查询失败")
        raise
