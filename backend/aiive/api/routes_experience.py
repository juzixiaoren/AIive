"""Owner-facing Experience review and observational effects."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.db.models import AgentTask, CapabilityPlan, ExperienceEpisode, ExperienceInsight, SelfDevRequest
from aiive.experience.capture import episode_visible
from aiive.experience.history import InvalidEpisodeCursor, episode_detail, episode_page
from aiive.experience.effects import ExperienceEffectEvaluator
from aiive.experience.feedback import record_feedback
from aiive.experience.insights import change_status, evidence_visible
from aiive.forget.concurrency import acquire_forget_shared_lock
from aiive.runtime.trace import Trace
from aiive.selfdev.improvement_discovery import CandidateNotFound, CandidateStateError, ImprovementDiscovery
from aiive.task_runtime.read_repository import TaskReadRepository

router = APIRouter(prefix="/api/experience")


class Feedback(BaseModel):
    kind: Literal["explicit_positive", "explicit_negative", "rework"]


class CandidateDecision(BaseModel):
    reason: str = Field(default="", max_length=300)


def _insight(db: Session, row: ExperienceInsight, *, detail: bool = False) -> dict[str, Any]:
    evaluator = ExperienceEffectEvaluator(db)
    data: dict[str, Any] = {
        "id": row.id, "insight_id": row.insight_id, "version": row.version, "kind": row.kind,
        "family": row.family, "title": row.title, "status": row.status, "operation": row.operation,
        "support_count": row.support_count, "refute_count": row.refute_count,
        "created_at": row.created_at, "enabled_at": row.enabled_at, "disabled_at": row.disabled_at,
    }
    if detail:
        data.update(body=row.body, evidence=row.evidence, usage=evaluator.usage(row), effect=evaluator.compare(row),
                    eligible=evidence_visible(db, row))
    return data


def _progress(db: Session, candidate: dict[str, Any]) -> dict[str, Any]:
    decision = candidate["decision"]
    task_id = decision.get("task_id")
    plan_id = decision.get("capability_plan_id")
    if task_id:
        task = db.get(AgentTask, task_id)
        reader = TaskReadRepository(db)
        if task is not None and reader.task_visible(task):
            latest = next((action for action in reader.actions(task_id)
                           if action.capability_id == "run_selfdev_repair"), None)
            delivery = (task.task_state or {}).get("workspace_delivery") or {}
            repair_status = None
            if latest is not None:
                repair_status = latest.status
                if latest.status == "succeeded" and delivery.get("verified") is True:
                    repair_status = "tests_passed"
            return {"kind": "selfdev", "id": task_id, "status": task.status,
                    "repair_status": repair_status}
    elif plan_id:
        plan = db.get(CapabilityPlan, plan_id)
        if plan is not None:
            return {"kind": "capability", "id": plan.id, "status": plan.status,
                    "selected": plan.selected_candidate}
    if decision.get("repair_request_id"):
        repair = db.get(SelfDevRequest, decision["repair_request_id"])
        if repair is not None:
            return {"kind": "selfdev", "status": repair.status, "repair_id": repair.id}
    if decision.get("memory_outcome"):
        return {"kind": "memory", "id": decision.get("memory_id"), "status": decision["memory_outcome"]}
    return {}


@router.get("")
def experience_home(
    limit: int = Query(30, ge=1, le=100), db: Session = Depends(get_db),
):
    acquire_forget_shared_lock(db)
    episodes = episode_page(db, limit)
    insights = db.query(ExperienceInsight).order_by(
        ExperienceInsight.created_at.desc(), ExperienceInsight.id,
    ).limit(limit * 3)
    candidates = ImprovementDiscovery.list_candidates(db, limit=limit)
    return {
        "episodes": episodes["items"], "episodes_next_cursor": episodes["next_cursor"],
        "insights": [_insight(db, row) for row in insights if evidence_visible(db, row, confirm_outcome=False)][:limit],
        "candidates": [{**candidate, "progress": _progress(db, candidate)} for candidate in candidates],
    }


@router.get("/episodes")
def list_episodes(
    limit: int = Query(30, ge=1, le=100), cursor: str | None = Query(None, max_length=100),
    db: Session = Depends(get_db),
):
    acquire_forget_shared_lock(db)
    try:
        return episode_page(db, limit, cursor)
    except InvalidEpisodeCursor as exc:
        raise HTTPException(422, "invalid_episode_cursor") from exc


def _get_episode(db: Session, episode_id: str, *, lock: bool = False) -> ExperienceEpisode:
    query = db.query(ExperienceEpisode).filter_by(id=episode_id)
    if lock:
        query = query.with_for_update()
    row = query.one_or_none()
    if row is None or not episode_visible(db, row):
        raise HTTPException(404, "episode_not_found")
    return row


@router.get("/episodes/{episode_id}")
def get_episode(episode_id: str, db: Session = Depends(get_db)):
    acquire_forget_shared_lock(db)
    return episode_detail(db, _get_episode(db, episode_id))


@router.post("/episodes/{episode_id}/feedback")
def post_feedback(episode_id: str, request: Feedback, db: Session = Depends(get_db)):
    acquire_forget_shared_lock(db)
    row = _get_episode(db, episode_id, lock=True)
    record_feedback(db, row, request.kind)
    result = episode_detail(db, row)
    db.commit()
    return result


@router.get("/insights/{insight_id}")
def get_insight(insight_id: str, db: Session = Depends(get_db)):
    row = db.get(ExperienceInsight, insight_id)
    if row is None or not evidence_visible(db, row, confirm_outcome=False):
        raise HTTPException(404, "insight_not_found")
    return _insight(db, row, detail=True)


@router.post("/insights/{insight_id}/{action}")
def review_insight(
    insight_id: str, action: Literal["accept", "reject", "enable", "disable"],
    db: Session = Depends(get_db),
):
    acquire_forget_shared_lock(db)
    try:
        row = change_status(db, insight_id, action)
        db.commit()
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _insight(db, row, detail=True)


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: str, db: Session = Depends(get_db)):
    try:
        candidate = ImprovementDiscovery.get_candidate(db, candidate_id)
    except CandidateNotFound as exc:
        raise HTTPException(404, "candidate_not_found") from exc
    return {**candidate, "progress": _progress(db, candidate)}


@router.post("/candidates/{candidate_id}/{action}")
def review_candidate(
    candidate_id: str, action: Literal["accept", "reject"],
    request: CandidateDecision, db: Session = Depends(get_db),
):
    try:
        if action == "accept":
            ImprovementDiscovery().accept(db, candidate_id, trace_id=Trace.new().trace_id)
        else:
            ImprovementDiscovery.reject(db, candidate_id, reason=request.reason)
    except CandidateNotFound as exc:
        raise HTTPException(404, "candidate_not_found") from exc
    except CandidateStateError as exc:
        raise HTTPException(409, str(exc)) from exc
    return get_candidate(candidate_id, db)
