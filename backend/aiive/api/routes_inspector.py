"""
API路由模块：诊断检查器
- 提供上下文构建运行详情和检索运行详情查询
- 提供事件诊断检查接口
"""
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.db.models import (
    ContextSnapshot,
    Event,
    RetrievalRun,
    RetrievalCandidate,
    MemoryRecallRun,
    MemoryRecallCandidate,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


@router.get("/context-runs/{trace_id}")
def get_context_run(trace_id: str, db: Session = Depends(get_db)):
    """获取指定 trace_id 的上下文构建运行详情

    Args:
        trace_id: 追踪ID
        db: 数据库会话

    Returns:
        上下文快照列表（包含前执行和后执行的全部上下文项）
    """
    try:
        snapshots = (
            db.query(ContextSnapshot)
            .filter(ContextSnapshot.trace_id == trace_id)
            .order_by(ContextSnapshot.created_at.desc())
            .limit(10)
            .all()
        )
        if not snapshots:
            raise HTTPException(
                status_code=404,
                detail={"code": "context_snapshot_not_found", "message": "未找到上下文快照"},
            )
        return {
            "trace_id": trace_id,
            "snapshots": [
                {
                    "stable_prefix_hash": s.stable_prefix_hash,
                    "context_items": s.context_items,
                    "meta": s.meta,
                    "token_total": s.token_total,
                }
                for s in snapshots
            ],
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("获取上下文构建运行详情失败: trace_id=%s", trace_id)
        raise


@router.get("/context-runs/{trace_id}/items/{item_id}")
def get_context_item_detail(trace_id: str, item_id: str, db: Session = Depends(get_db)):
    """获取指定上下文项的完整内容（懒加载）

    Args:
        trace_id: 追踪ID
        item_id: 上下文项ID（如 agent_output、tool_call:0、tool_result:1）
        db: 数据库会话

    Returns:
        {"item_id": ..., "full_content": "..."}
    """
    try:
        snapshot = (
            db.query(ContextSnapshot)
            .filter(ContextSnapshot.trace_id == trace_id)
            .order_by(ContextSnapshot.created_at.desc())
            .first()
        )
        if not snapshot:
            raise HTTPException(
                status_code=404,
                detail={"code": "context_snapshot_not_found", "message": "未找到上下文快照"},
            )
        full_contents: dict[str, Any] = snapshot.meta.get("full_contents", {}) if snapshot.meta else {}
        content = full_contents.get(item_id)
        if content is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "context_item_not_found", "message": "未找到上下文项"},
            )
        return {"item_id": item_id, "full_content": content}
    except HTTPException:
        raise
    except Exception:
        logger.exception("获取上下文项详情失败: trace_id=%s, item_id=%s", trace_id, item_id)
        raise


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
            return {"error": "not found"}

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
            return {"error": "not found"}
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
        events = q.order_by(Event.created_at.asc()).limit(limit).all()
        return [
            {
                "id": e.id,
                "trace_id": e.trace_id,
                "event_type": e.event_type,
                "payload": e.payload,
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ]
    except Exception:
        logger.exception("诊断事件查询失败")
        raise
