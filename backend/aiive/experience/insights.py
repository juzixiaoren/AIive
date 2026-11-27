"""Validate and version incremental experience operations."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import ExperienceEpisode, ExperienceInsight, ExperienceSignal
from aiive.experience.capture import NEGATIVE, POSITIVE, episode_visible, result_for, visible_signals
from aiive.experience.families import match_terms, stable_id
from aiive.forget.durable_sources import references_visible

OPERATIONS = {"ADD", "EDIT", "UPVOTE", "DOWNVOTE", "DEPRECATE"}
KINDS = {"playbook", "skill", "tool_hint"}
CAPABILITY = re.compile(r"[a-zA-Z0-9_.:-]{1,128}")


def usage_ref(row: ExperienceInsight) -> dict[str, Any]:
    return {"id": row.insight_id, "version": row.version, "kind": row.kind}


def independent_key(episode: ExperienceEpisode) -> str:
    return episode.parent_id or episode.id


def evidence_visible(db: Session, insight: ExperienceInsight, *, confirm_outcome: bool = True) -> bool:
    if not insight.evidence:
        return False
    for ref in insight.evidence:
        episode = db.get(ExperienceEpisode, ref["episode_id"])
        if episode is None or not episode_visible(db, episode):
            return False
        if ref["role"] == "discovery":
            from aiive.db.models import AgentAction, AgentTaskEvent
            from aiive.task_runtime.read_repository import TaskReadRepository

            miss = db.get(AgentTaskEvent, ref["source_id"])
            recovery = db.get(AgentTaskEvent, ref["recovery_source_id"])
            action = db.get(AgentAction, ref["action_id"])
            reader = TaskReadRepository(db)
            if (miss is None or recovery is None or action is None
                    or miss.task_id != episode.source_id or recovery.task_id != episode.source_id
                    or action.task_id != episode.source_id or action.status != "succeeded"
                    or action.capability_id != insight.body.get("capability_id")
                    or miss.payload.get("experience_classification") != "search_no_results"
                    or recovery.payload.get("capability_id") != insight.body.get("capability_id")
                    or not recovery.payload.get("in_scope")
                    or len(reader.filter_children("agent_task_event", [miss, recovery])) != 2
                    or not reader.visible_child("agent_action", action)):
                return False
        else:
            signals = visible_signals(db, episode.id)
            if ref["source_id"] != episode.source_id and not any(
                signal.source_id == ref["source_id"] for signal in signals
            ):
                return False
            if not _trace_reference_visible(db, episode, ref["trace_source_id"], signals):
                return False
            if confirm_outcome and result_for(episode, signals) != ref["outcome"]:
                return False
    return True


def source_payload(db: Session, uses: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Flatten inputs for existing Forget guards on generated content."""
    sources: dict[str, set[str]] = {"turn_record": set(), "event": set(), "memory_record": set()}
    from aiive.db.forget_models import ContentProvenanceRef
    for use in uses:
        if use.get("builtin"):
            continue
        row = db.query(ExperienceInsight).filter_by(insight_id=use["id"], version=use["version"]).one_or_none()
        if row is None or not evidence_visible(db, row):
            raise ValueError("experience_source_unavailable")
        for ref in row.evidence:
            episode = db.get(ExperienceEpisode, ref["episode_id"])
            assert episode is not None
            if episode.kind == "turn":
                sources["turn_record"].add(episode.source_id)
                owner = "experience_turn_context"
            else:
                owner = "agent_task"
            for source in db.query(ContentProvenanceRef).filter_by(owner_type=owner, owner_id=episode.source_id):
                if source.source_type not in sources:
                    raise ValueError("experience_source_unavailable")
                sources[source.source_type].add(source.source_id)
            signal = db.get(ExperienceSignal, ref.get("signal_id")) if ref.get("signal_id") else None
            if signal is not None and signal.details.get("source_type") == "event":
                sources["event"].add(signal.source_id)
    if not references_visible(db, sources):
        raise ValueError("experience_source_unavailable")
    return {key: sorted(ids) for key, ids in sources.items() if ids}


def _text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _strings(value: Any, limit: int, count: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(text for item in value[:count] if (text := _text(item, limit))))


def _workflow(raw: dict[str, Any]) -> dict[str, Any] | None:
    body = {
        "conditions": _text(raw.get("conditions"), 220),
        "match_terms": _strings(raw.get("match_terms"), 48, 8),
        "steps": _strings(raw.get("steps"), 190, 6),
        "required_capabilities": _strings(raw.get("required_capabilities"), 128, 10),
        "expected_result": _text(raw.get("expected_result"), 180),
        "verification": _text(raw.get("verification"), 220),
        "recovery": _text(raw.get("recovery"), 220),
        "root_cause": _text(raw.get("root_cause"), 220),
    }
    if not all(body[key] for key in ("conditions", "match_terms", "steps", "expected_result", "verification", "recovery")):
        return None
    if any(not CAPABILITY.fullmatch(value) for value in body["required_capabilities"]):
        return None
    return body


def _trace_reference_visible(
    db: Session, episode: ExperienceEpisode, source_id: str, signals: list[ExperienceSignal],
) -> bool:
    if source_id == episode.source_id or any(signal.source_id == source_id for signal in signals):
        return True
    if episode.kind == "turn":
        return source_id in episode.refs.get("event_ids", []) and references_visible(db, {"event": [source_id]})

    from aiive.db.models import AgentAction
    from aiive.task_runtime.read_repository import TaskReadRepository

    reader = TaskReadRepository(db)
    if source_id in episode.refs.get("action_ids", []):
        action = db.get(AgentAction, source_id)
        return bool(action and action.task_id == episode.source_id and reader.visible_child("agent_action", action))
    if source_id in episode.refs.get("evidence_ids", []):
        return reader.get_evidence(episode.source_id, source_id) is not None
    return False


def _find_evidence(db: Session, refs: Any, allowed: dict[str, dict[str, Any]], family: str) -> list[dict[str, Any]]:
    if not isinstance(refs, list) or not 1 <= len(refs) <= 12:
        return []
    result = {}
    for ref in refs:
        if not isinstance(ref, dict) or not isinstance(ref.get("episode_id"), str):
            return []
        episode_id = ref["episode_id"]
        observed = allowed.get(episode_id)
        if observed is None or observed["family"] != family:
            return []
        episode = db.get(ExperienceEpisode, episode_id)
        if episode is None or not episode_visible(db, episode):
            return []
        source_id = ref.get("source_id")
        if not isinstance(source_id, str) or source_id not in observed["source_ids"]:
            return []
        signals = visible_signals(db, episode.id)
        outcome = result_for(episode, signals)
        if outcome != observed["outcome"]:
            return []
        if not _trace_reference_visible(db, episode, source_id, signals):
            return []
        role = "support" if outcome in POSITIVE else "refute" if outcome in NEGATIVE else "comparison"
        signal = next((s for s in signals if s.source_id == source_id and s.kind == outcome), None)
        # Include the actual signal driving the assessment as a durable dependency.
        outcome_signal = signal or next(
            (s for s in signals if s.kind == outcome or (outcome == "corrected" and s.kind == "explicit_negative")),
            None,
        )
        proof_id = outcome_signal.source_id if outcome_signal else episode.source_id
        result[episode_id] = {
            "episode_id": episode_id, "source_id": proof_id, "trace_source_id": source_id,
            "signal_id": outcome_signal.id if outcome_signal else None, "outcome": outcome,
            "role": role, "independent_key": independent_key(episode),
        }
    return list(result.values())


def _store_revision(
    db: Session, *, key: str, kind: str, family: str, title: str,
    operation: str, body: dict[str, Any], evidence: list[dict[str, Any]],
) -> ExperienceInsight | None:
    history = db.query(ExperienceInsight).filter_by(insight_id=key).order_by(
        ExperienceInsight.version.desc(),
    ).with_for_update().all()
    latest = history[0] if history else None
    current = next((row for row in history if row.status in {"active", "disabled"}), None)
    evidence_keys = {(item["episode_id"], item["source_id"], item["outcome"]) for item in evidence}
    if operation == "ADD" and (current is not None or any(row.status == "pending" for row in history)):
        return None
    if operation != "ADD" and (current is None or any(row.status == "pending" for row in history)):
        return None
    for revision in history:
        prior = {(item["episode_id"], item["source_id"], item["outcome"]) for item in revision.evidence}
        if operation == revision.operation and body == revision.body and evidence_keys <= prior:
            return None
    row = ExperienceInsight(
        id=stable_id("revision", key, str(latest.version + 1 if latest else 1)),
        insight_id=key, version=latest.version + 1 if latest else 1,
        kind=kind, family=family, title=title, body=body, evidence=evidence,
        operation=operation, status="pending",
        support_count=len({item["independent_key"] for item in evidence if item["role"] in {"support", "discovery"}}),
        refute_count=len({item["independent_key"] for item in evidence if item["role"] == "refute"}),
    )
    db.add(row)
    db.flush()
    return row


def curate(db: Session, raw: Any, allowed: dict[str, dict[str, Any]]) -> ExperienceInsight | None:
    if not isinstance(raw, dict):
        return None
    kind, operation, family = (_text(raw.get(field), 64) for field in ("kind", "op", "family"))
    if operation not in OPERATIONS or kind not in {"playbook", "skill"} or not family:
        return None
    key = _text(raw.get("insight_id"), 36)
    current = None
    if operation != "ADD":
        current = db.query(ExperienceInsight).filter(
            ExperienceInsight.insight_id == key, ExperienceInsight.family == family,
            ExperienceInsight.kind == kind, ExperienceInsight.status.in_(("active", "disabled")),
        ).order_by(ExperienceInsight.version.desc()).first()
        if current is None or not evidence_visible(db, current, confirm_outcome=operation != "EDIT"):
            return None
    evidence = _find_evidence(db, raw.get("evidence"), allowed, family)
    if not evidence:
        return None
    supporting = {item["independent_key"] for item in evidence if item["role"] == "support"}
    refuting = {item["independent_key"] for item in evidence if item["role"] == "refute"}
    title = _text(raw.get("title"), 180)
    body = _workflow(raw)
    if operation in {"ADD", "EDIT"}:
        if body is None or not title or len(supporting - refuting) < 2:
            return None
        if kind == "playbook" and not refuting:
            return None
        positive_terms = [set(allowed[item["episode_id"]]["terms"]) for item in evidence
                          if item["role"] == "support" and item["independent_key"] not in refuting]
        phrases = {frozenset(match_terms(term)) for term in body["match_terms"]} - {frozenset()}
        if sum(len(set().union(*(phrase for phrase in phrases if phrase <= terms))) >= 2
               for terms in positive_terms) < 2:
            return None
        if operation == "ADD":
            key = stable_id("insight", kind, family, ",".join(sorted(" ".join(sorted(phrase)) for phrase in phrases)))
            candidates = db.query(ExperienceInsight).filter(
                ExperienceInsight.family == family, ExperienceInsight.kind == kind,
                ExperienceInsight.status.in_(("active", "disabled", "pending")),
            )
            if any(row.body.get("steps") == body["steps"] and
                   row.body.get("verification") == body["verification"] for row in candidates):
                return None
        confirmed_capabilities = set().union(*(set(allowed[e["episode_id"]]["capabilities"])
                                              for e in evidence if e["role"] == "support"))
        if not set(body["required_capabilities"]) <= confirmed_capabilities:
            return None
    else:
        assert current is not None
        body, title = current.body, current.title
        old = {item["independent_key"] for item in current.evidence}
        applied = set()
        for item in evidence:
            episode = db.get(ExperienceEpisode, item["episode_id"])
            if episode and any(use["id"] == key and use["version"] == current.version for use in episode.uses):
                applied.add(item["independent_key"])
        if operation == "UPVOTE" and not (supporting & applied) - old:
            return None
        if operation == "DOWNVOTE" and not (refuting & applied) - old:
            return None
        if operation == "DEPRECATE" and len((refuting & applied) - old) < 2:
            return None
        evidence = list({item["episode_id"]: item for item in [*current.evidence, *evidence]}.values())
    return _store_revision(db, key=key, kind=kind, family=family, title=title, operation=operation,
                           body=body, evidence=evidence)


def propose_tool_hint(db: Session, family: str, query: str, capability: str, evidence: list[dict[str, Any]]) -> ExperienceInsight | None:
    if not CAPABILITY.fullmatch(capability) or not 2 <= len(query) <= 100:
        return None
    if len({item["independent_key"] for item in evidence}) < 2:
        return None
    return _store_revision(
        db, key=stable_id("tool_hint", family, capability, query), kind="tool_hint", family=family,
        title=f"{capability} · {query}", operation="ADD",
        body={"capability_id": capability, "alias": query}, evidence=evidence,
    )


def change_status(db: Session, identity: str, action: str, now: datetime | None = None) -> ExperienceInsight:
    target = db.get(ExperienceInsight, identity)
    if target is None:
        raise LookupError("insight_not_found")
    history = db.query(ExperienceInsight).filter_by(insight_id=target.insight_id).order_by(
        ExperienceInsight.version.desc(),
    ).with_for_update().populate_existing().all()
    row = next(revision for revision in history if revision.id == identity)
    expected = {"accept": "pending", "reject": "pending", "enable": "disabled", "disable": "active"}
    if expected.get(action) != row.status:
        raise ValueError("invalid_insight_transition")
    now = now or datetime.now(UTC)
    if action in {"accept", "enable"} and not evidence_visible(db, row):
        raise ValueError("insight_evidence_unavailable")
    if action == "accept":
        for old in history:
            if old.status in {"active", "disabled"}:
                old.status = "superseded"
                if old.disabled_at is None:
                    old.disabled_at = now
        row.status = "deprecated" if row.operation == "DEPRECATE" else "active"
        if row.status == "active":
            row.enabled_at = now
    elif action == "enable":
        row.status, row.enabled_at, row.disabled_at = "active", now, None
    elif action == "disable":
        row.status, row.disabled_at = "disabled", now
    else:
        row.status = "rejected"
    return row
