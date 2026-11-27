"""Route accepted findings through the existing capability and memory domains."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aiive.db.models import AgentTaskEvent, Event, ExperienceEpisode, SelfDevRequest
from aiive.experience.capture import episode_visible, known_capability
from aiive.experience.families import stable_id
from aiive.forget.durable_sources import references_visible

_PREFERENCE = re.compile(
    r"^(?:以后(?:回复|回答)(?:时)?请|今后(?:回复|回答)(?:时)?请|"
    + r"(?:in future|from now on)[, ]+(?:please )?(?:reply|respond|answer)\b)", re.I,
)


def _create_candidate(db: Session, key: str, *, route: str, title: str, goal: str, refs: list[dict[str, Any]]) -> str | None:
    identity = stable_id("candidate", route, key)
    if db.get(SelfDevRequest, identity) is not None:
        return None
    payload = {
        "candidate_id": identity, "title": title, "problem": title, "repair_goal": goal,
        "target": goal, "route": route, "experience_refs": refs,
        "evidence_ids": [item["source_id"] for item in refs], "occurrences": len(refs),
        "discovered_at": datetime.now(UTC).isoformat(),
    }
    try:
        with db.begin_nested():
            db.add(SelfDevRequest(id=identity, status="candidate_pending", trace_id=identity,
                                  goal=goal, plan={"candidate": payload}))
            db.flush()
    except IntegrityError:
        return None
    return identity


def propose_capability_gap(db: Session, family: str, capability: str, refs: list[dict[str, Any]]) -> str | None:
    distinct = list({item["independent_key"]: item for item in refs}.values())[:12]
    if len(distinct) < 2 or known_capability(db, capability):
        return None
    return _create_candidate(
        db, f"{family}:{capability}", route="mcp", title=f"能力目录缺少 {capability}",
        goal=capability, refs=distinct,
    )


def propose_preferences(db: Session, episodes: list[ExperienceEpisode]) -> list[str]:
    results = []
    for episode in episodes:
        if episode.kind != "turn":
            continue
        event = db.get(Event, episode.refs["request_event_id"])
        if event is None or not preference_text(event):
            continue
        identity = _create_candidate(
            db, event.id, route="memory", title="回复方式偏好", goal="将用户明确说明的回复方式写入记忆",
            refs=[{"episode_id": episode.id, "source_id": event.id}],
        )
        if identity:
            results.append(identity)
    return results


def preference_text(event: Event) -> str:
    text = str(event.payload.get("content") or "").strip()
    return text if len(text) <= 300 and _PREFERENCE.search(text) else ""


def candidate_visible(db: Session, payload: dict[str, Any]) -> bool:
    route = payload.get("route", "code")
    if route == "code":
        return True
    if route not in {"mcp", "memory"}:
        return False
    refs = payload.get("experience_refs") or []
    if not refs:
        return False
    for ref in refs:
        episode = db.get(ExperienceEpisode, ref["episode_id"])
        if episode is None or not episode_visible(db, episode):
            return False
        if route == "memory":
            if episode.refs.get("request_event_id") != ref["source_id"] or not references_visible(
                db, {"event": [ref["source_id"]]},
            ):
                return False
        else:
            event = db.get(AgentTaskEvent, ref["source_id"])
            if (event is None or event.task_id != episode.source_id
                    or event.payload.get("experience_classification") != "capability_missing_from_catalog"
                    or event.payload.get("capability_id") != payload.get("target")):
                return False
    return True


def preference_preview(db: Session, payload: dict[str, Any]) -> str:
    if payload.get("route") != "memory" or not candidate_visible(db, payload):
        return ""
    event = db.get(Event, payload["experience_refs"][0]["source_id"])
    return preference_text(event) if event else ""


def accept_routed(db: Session, row: SelfDevRequest, payload: dict[str, Any], trace_id: str) -> dict[str, Any]:
    from aiive.selfdev.improvement_discovery import CandidateStateError

    route = payload["route"]
    if not candidate_visible(db, payload):
        raise CandidateStateError("candidate_evidence_unavailable")
    if route == "mcp":
        from aiive.tools.builtin_mcp import persist_capability_plan

        capability = payload["target"]
        if known_capability(db, capability):
            raise CandidateStateError("capability_already_available")
        result = persist_capability_plan(db, f"为 {capability} 查找对应的 MCP 能力")
        return {"capability_plan_id": result["plan_id"]}
    from aiive.context.run_context import RUN_CTX_MANUAL_MEMORY_API, RunContext
    from aiive.memory.memory_types import TrustLevel, WriteOutcome
    from aiive.memory.memory_write_service import MemoryWriteService
    from aiive.memory.proposal_normalizer import ProposalNormalizer

    content = preference_preview(db, payload)
    if not content:
        raise CandidateStateError("preference_source_unavailable")
    ref = payload["experience_refs"][0]
    source = db.get(Event, ref["source_id"])
    assert source is not None
    normalized = ProposalNormalizer().normalize(
        content=content, memory_type_hint="user_profile", memory_key_hint="user.preference.response_style",
        confidence=0.95, importance=0.8, trust_level=TrustLevel.TRUSTED.value,
        evidence=[], source_event_ids=[source.id], extractor_name="experience",
        extractor_version="1.0", thread_id=source.thread_id,
    )
    proposal = normalized.proposal
    if proposal is None:
        raise CandidateStateError("preference_normalization_failed")
    proposal.execution_mode = "user_required"
    proposal.idempotency_key = f"experience:{row.id}"
    result = MemoryWriteService(db).write(proposal, run_context=RunContext(
        thread_id=source.thread_id, trace_id=trace_id, source=RUN_CTX_MANUAL_MEMORY_API,
        source_event_ids=[source.id], execution_mode="user_required",
    ))
    if result.outcome in {WriteOutcome.GATE_REJECTED, WriteOutcome.FAILED}:
        raise CandidateStateError("preference_write_unavailable")
    return {"memory_id": result.memory_id, "memory_outcome": result.outcome.value}
