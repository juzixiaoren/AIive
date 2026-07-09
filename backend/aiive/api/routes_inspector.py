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

router = APIRouter(prefix="/api")


@router.get("/context-runs/{trace_id}")
def get_context_run(trace_id: str, db: Session = Depends(get_db)):
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


@router.get("/retrieval-runs/{run_id}")
def get_retrieval_run(run_id: str, db: Session = Depends(get_db)):
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


@router.get("/inspector/events")
def inspector_events(
    thread_id: str | None = Query(None),
    trace_id: str | None = Query(None),
    limit: int = Query(50),
    db: Session = Depends(get_db),
):
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
