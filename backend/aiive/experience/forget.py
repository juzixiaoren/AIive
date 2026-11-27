"""Scrub learned text and projections in the same Forget content transaction."""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from aiive.db.forget_models import ContentProvenanceRef
from aiive.db.models import ExperienceEpisode, ExperienceInsight, ExperienceSignal, SelfDevRequest

if TYPE_CHECKING:
    from aiive.forget.content_scope import ContentScope


def scrub_experience(db: Session, scope: ContentScope) -> dict[str, int]:
    ref = ContentProvenanceRef
    sources = {"event": scope.event_ids, "turn_record": scope.turn_tokens, "memory_record": scope.memory_ids}
    conditions = [and_(ref.source_type == kind, ref.source_id.in_(ids)) for kind, ids in sources.items() if ids]
    derived_turns = (db.query(ref.owner_id).filter(ref.owner_type == "experience_turn_context", or_(*conditions))
                     if conditions else ())
    episodes = db.query(ExperienceEpisode).filter(
        ExperienceEpisode.completed_at <= scope.cutoff,
        or_(
            and_(ExperienceEpisode.kind == "turn",
                 or_(ExperienceEpisode.source_id.in_(scope.turn_tokens),
                     ExperienceEpisode.source_id.in_(derived_turns))),
            and_(ExperienceEpisode.kind == "task",
                 ExperienceEpisode.source_id.in_(scope.agent_task_ids | scope.partial_agent_task_ids)),
        ),
    ).all()
    affected = {episode.id for episode in episodes}
    observations = db.query(ExperienceSignal).filter(
        or_(ExperienceSignal.episode_id.in_(affected), ExperienceSignal.source_id.in_(scope.event_ids)),
    ).all()
    signal_ids = {signal.source_id for signal in observations}
    # A standalone feedback message does not remove its target Episode.
    for signal in observations:
        row = db.get(ExperienceEpisode, signal.episode_id)
        if row is not None and row.id not in affected:
            row.reflected_at = None
        db.delete(signal)
    for episode in episodes:
        db.delete(episode)
    removed = 0
    for insight in db.query(ExperienceInsight).filter(
        ExperienceInsight.created_at <= scope.cutoff, ExperienceInsight.status != "redacted",
    ):
        if not any(item["episode_id"] in affected or item["source_id"] in signal_ids for item in insight.evidence):
            continue
        insight.title, insight.body, insight.evidence = "", {}, []
        insight.support_count, insight.refute_count = 0, 0
        insight.status = "redacted"
        removed += 1
    for row in db.query(SelfDevRequest).filter(
        SelfDevRequest.status.startswith("candidate_"), SelfDevRequest.created_at <= scope.cutoff,
    ):
        payload = (row.plan or {}).get("candidate") or {}
        if payload.get("route") not in {"memory", "mcp"}:
            continue
        if any(item["episode_id"] in affected or item["source_id"] in scope.event_ids
               for item in payload.get("experience_refs", [])):
            row.goal = "[redacted]"
            row.plan = {"candidate": {"route": "redacted"}}
    return {"experience_episodes": len(episodes), "experience_signals": len(observations),
            "experience_insights": removed}
