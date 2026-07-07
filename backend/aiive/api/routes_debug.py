from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.db.models import ContextSnapshot, Event, LLMCall

router = APIRouter(prefix="/api/debug")


@router.get("/events")
def list_events(
    trace_id: str | None = Query(None),
    thread_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(Event)
    if trace_id:
        q = q.filter(Event.trace_id == trace_id)
    if thread_id:
        q = q.filter(Event.thread_id == thread_id)
    events = q.order_by(Event.created_at.asc()).limit(100).all()
    return [
        {
            "id": e.id,
            "trace_id": e.trace_id,
            "thread_id": e.thread_id,
            "event_type": e.event_type,
            "payload": e.payload,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]


@router.get("/llm_calls")
def list_llm_calls(
    trace_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(LLMCall)
    if trace_id:
        q = q.filter(LLMCall.trace_id == trace_id)
    calls = q.order_by(LLMCall.created_at.asc()).limit(100).all()
    return [
        {
            "id": c.id,
            "trace_id": c.trace_id,
            "thread_id": c.thread_id,
            "model": c.model,
            "latency_ms": c.latency_ms,
            "input_preview": c.input_preview,
            "output_preview": c.output_preview,
            "created_at": c.created_at.isoformat(),
        }
        for c in calls
    ]


@router.get("/traces/{trace_id}")
def get_trace(trace_id: str, db: Session = Depends(get_db)):
    snapshot = (
        db.query(ContextSnapshot)
        .filter(ContextSnapshot.trace_id == trace_id)
        .first()
    )
    if not snapshot:
        return JSONResponse(
            status_code=404,
            content={"error": "trace not found", "trace_id": trace_id},
        )

    events = (
        db.query(Event)
        .filter(Event.trace_id == trace_id)
        .order_by(Event.created_at.asc())
        .all()
    )

    llm_call = (
        db.query(LLMCall)
        .filter(LLMCall.trace_id == trace_id)
        .first()
    )

    return {
        "trace_id": trace_id,
        "thread_id": snapshot.thread_id,
        "snapshot": {
            "stable_prefix_hash": snapshot.stable_prefix_hash,
            "context_items": snapshot.context_items,
            "meta": snapshot.meta,
        },
        "events": [
            {
                "event_type": e.event_type,
                "payload": e.payload,
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ],
        "llm_call": {
            "model": llm_call.model,
            "latency_ms": llm_call.latency_ms,
            "input_preview": llm_call.input_preview,
            "output_preview": llm_call.output_preview,
        } if llm_call else None,
    }
