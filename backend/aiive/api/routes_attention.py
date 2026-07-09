from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.runtime.attention_manager import AttentionManager
from aiive.runtime.rhythm_manager import RhythmManager

router = APIRouter(prefix="/api")


class RecomputeRequest(BaseModel):
    thread_id: str
    topic: str = ""


@router.get("/attention/current")
def get_attention(thread_id: str = Query(...), db: Session = Depends(get_db)):
    mgr = AttentionManager(db)
    state = mgr.get_current(thread_id)
    if not state:
        return {"thread_id": thread_id, "decision": "continue"}
    return {
        "thread_id": state.thread_id,
        "decision": state.decision,
        "suggestion": state.suggestion,
        "focus_topic": state.focus_topic,
        "recent_topics": state.recent_topics,
    }


@router.post("/attention/recompute")
def recompute(request: RecomputeRequest, db: Session = Depends(get_db)):
    mgr = AttentionManager(db)
    return mgr.recompute(request.thread_id, request.topic)


@router.get("/rhythm/daily")
def daily_rhythm(db: Session = Depends(get_db)):
    mgr = RhythmManager(db)
    return mgr.daily_summary()


@router.get("/rhythm/weekly")
def weekly_rhythm(db: Session = Depends(get_db)):
    mgr = RhythmManager(db)
    return mgr.weekly_summary()
