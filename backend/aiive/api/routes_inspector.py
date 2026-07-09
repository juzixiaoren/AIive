"""
API路由模块：诊断检查器
- 提供上下文构建运行详情和检索运行详情查询
- 提供事件诊断检查接口
"""
import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.db.models import (
    ContextSnapshot,
    Event,
    LLMCall,
    RetrievalRun,
    RetrievalCandidate,
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
        上下文快照列表和LLM调用记录
    """
    try:
        snapshots = (
            db.query(ContextSnapshot)
            .filter(ContextSnapshot.trace_id == trace_id)
            .order_by(ContextSnapshot.created_at.desc())
            .limit(10)
            .all()
        )
        llm_calls = (
            db.query(LLMCall)
            .filter(LLMCall.trace_id == trace_id)
            .limit(10)
            .all()
        )
        return {
            "trace_id": trace_id,
            "snapshots": [
                {
                    "stable_prefix_hash": s.stable_prefix_hash,
                    "context_items": s.context_items,
                    "meta": s.meta,
                }
                for s in snapshots
            ],
            "llm_calls": [
                {"model": c.model, "latency_ms": c.latency_ms}
                for c in llm_calls
            ],
        }
    except Exception:
        logger.exception("获取上下文构建运行详情失败: trace_id=%s", trace_id)
        raise


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
                {"chunk_id": c.chunk_id, "source": c.source, "score": c.score}
                for c in candidates
            ],
        }
    except Exception:
        logger.exception("获取检索运行详情失败: run_id=%s", run_id)
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
