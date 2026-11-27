"""Build deterministic Experience projections from committed runtime records."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import (
    AgentRun, AgentTask, AgentTaskEvent, Capability, DesktopNode, Event, ExperienceEpisode,
    ExperienceSignal, OutboxJob, TurnRecord,
)
from aiive.experience.families import family_for, merge_uses, stable_id
from aiive.forget.durable_sources import references_visible, sources_visible
from aiive.task_runtime.read_repository import TaskReadRepository

JOB_CAPTURE = "experience_capture"
POSITIVE = frozenset({"verified_success", "explicit_positive"})
NEGATIVE = frozenset({"corrected", "rework", "failed", "partial"})
TOOL_EXECUTED = frozenset({"completed", "succeeded", "failed", "execution_unknown"})


def enqueue_capture(
    db: Session, *, turn: TurnRecord | None = None, task: AgentTask | None = None,
    trace_id: str | None = None, snapshot_id: str | None = None, uses: list[dict[str, Any]] | None = None,
) -> None:
    if turn is not None:
        refs = {"turn_record_id": turn.id, "context_snapshot_id": snapshot_id, "uses": uses or []}
        key = f"turn:{turn.id}"
    else:
        assert task is not None
        refs = {"task_id": task.id}
        key = f"task:{task.id}"
    db.add(OutboxJob(
        operation_id=f"{JOB_CAPTURE}:{key}", job_type=JOB_CAPTURE, status="pending",
        max_retries=3, trace_id=trace_id, payload={"schema_version": 1, **refs},
    ))


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def turn_visible(db: Session, row: TurnRecord) -> bool:
    return references_visible(db, {"turn_record": [row.id]})


def episode_visible(db: Session, episode: ExperienceEpisode) -> bool:
    if episode.kind == "turn":
        if not references_visible(db, {"turn_record": [episode.source_id]}):
            return False
        if episode.refs.get("experience_context"):
            return sources_visible(db, "experience_turn_context", episode.source_id)
        return True
    task = db.get(AgentTask, episode.source_id)
    return task is not None and TaskReadRepository(db).task_visible(task)


def add_signal(
    db: Session, episode: ExperienceEpisode, kind: str, source_id: str,
    details: dict[str, Any] | None = None, *, identity: str | None = None,
    created_at: datetime | None = None,
) -> ExperienceSignal:
    key = stable_id("signal", identity or f"{episode.id}:{source_id}:{kind}")
    signal = db.get(ExperienceSignal, key)
    if signal is None:
        signal = ExperienceSignal(
            id=key, episode_id=episode.id, kind=kind, source_id=source_id,
            details=details or {}, created_at=created_at or datetime.now(UTC),
        )
        db.add(signal)
    elif identity and (signal.kind != kind or signal.episode_id != episode.id):
        signal.kind, signal.episode_id = kind, episode.id
        signal.details = details or {}
        signal.created_at = created_at or datetime.now(UTC)
    return signal


def visible_signals(db: Session, episode_id: str) -> list[ExperienceSignal]:
    rows = db.query(ExperienceSignal).filter_by(episode_id=episode_id).order_by(
        ExperienceSignal.created_at, ExperienceSignal.id,
    ).all()
    return [row for row in rows if row.details.get("source_type") != "event"
            or references_visible(db, {"event": [row.source_id]})]


def result_for(episode: ExperienceEpisode, signals: list[ExperienceSignal]) -> str:
    kinds = {signal.kind for signal in signals}
    for kind in ("rework", "corrected", "explicit_negative"):
        if kind in kinds:
            return "rework" if kind == "rework" else "corrected"
    if "explicit_positive" in kinds and episode.outcome != "verified_success":
        return "explicit_positive"
    return episode.outcome


def build_episode(db: Session, payload: dict[str, Any]) -> ExperienceEpisode | None:
    """The source lock serializes capture retries without duplicating signals."""
    turn_id, task_id = payload.get("turn_record_id"), payload.get("task_id")
    if bool(turn_id) == bool(task_id):
        raise ValueError("one_experience_source_required")
    kind, model = ("turn", TurnRecord) if turn_id else ("task", AgentTask)
    source_id = turn_id or task_id
    if not isinstance(source_id, str):
        raise ValueError("invalid_experience_source_id")
    source = db.query(model).filter(model.id == source_id).with_for_update().one_or_none()
    if source is None:
        return None
    identity = stable_id(kind, source_id)
    existing = db.get(ExperienceEpisode, identity)
    if existing is not None:
        return existing
    if isinstance(source, TurnRecord):
        episode, discoveries = _build_turn(db, source, identity, payload)
    else:
        episode, discoveries = _build_task(db, source, identity)
    if episode is None:
        return None
    db.add(episode)
    db.flush()
    if episode.outcome != "unknown":
        evidence = episode.refs.get("verification_event_id") or source_id
        add_signal(db, episode, episode.outcome, evidence, created_at=episode.completed_at)
    add_signal(db, episode, "execution_efficiency", source_id, created_at=episode.completed_at)
    for event, classification in discoveries:
        if classification:
            add_signal(
                db, episode, classification, event.id,
                {"source_type": "event" if isinstance(event, Event) else "agent_task_event"},
                created_at=event.created_at,
            )
    return episode


def _build_turn(
    db: Session, turn: TurnRecord, identity: str, payload: dict[str, Any],
) -> tuple[ExperienceEpisode | None, list[tuple[Event, str | None]]]:
    if turn.status != "completed" or turn.cancel_requested_at or not turn_visible(db, turn):
        return None, []
    events = db.query(Event).filter_by(thread_id=turn.thread_id, turn_id=turn.turn_id).order_by(
        Event.turn_event_index, Event.id,
    ).all()
    request = next((event for event in events if event.id == turn.request_event_id), None)
    reply_id = (turn.response_payload or {}).get("event_id")
    if turn.source != "user" or request is None or not reply_id:
        return None, []
    close = next((event for event in reversed(events) if event.event_type == "chat_ended"), None)
    usage = (close.payload.get("llm_usage") or {}) if close is not None else {}
    tools = [event for event in events if event.event_type == "tool_result"
             and not event.payload.get("protocol_only") and event.payload.get("status") in TOOL_EXECUTED]
    searches = misses = 0
    discovery_uses = []
    discoveries = []
    for event in tools:
        if event.payload.get("name") != "search_worker_capabilities":
            continue
        result = event.payload.get("result") or {}
        if not isinstance(result, dict):
            continue
        result = result.get("result", result)
        if not isinstance(result, dict):
            continue
        if "tools" in result and "status" in result:
            matched = [item["capability_id"] for item in result["tools"]]
            classification = discovery_classification(db, "tool_search", {"matched": matched, "status": result["status"]})
            if classification != "discovery_temporary_failure":
                searches += 1
                misses += classification == "search_no_results"
            discovery_uses.extend(result.get("experience_uses", []))
        elif "available" in result:
            classification = discovery_classification(db, "tool_describe", {
                "found": result["available"], "in_scope": result["available"],
                "capability_id": result["capability_id"],
            })
        elif event.payload.get("status") == "failed":
            classification = "discovery_temporary_failure"
        else:
            continue
        discoveries.append((event, classification))
    uses = merge_uses(payload.get("uses") or [], discovery_uses)
    return ExperienceEpisode(
        id=identity, kind="turn", source_id=turn.id, thread_id=turn.thread_id,
        task_type="chat", family=family_for(str(request.payload.get("content") or "")), outcome="unknown",
        refs={"event_ids": [event.id for event in events], "request_event_id": request.id,
              "assistant_event_id": reply_id, "context_snapshot_id": payload.get("context_snapshot_id"),
              "experience_context": bool(uses)},
        uses=uses,
        metrics={"actions": len(tools), "tool_calls": len(tools),
                 "tool_failures": sum(event.payload.get("status") == "failed" for event in tools),
                 "searches": searches, "search_misses": misses,
                 "tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                 if usage.get("calls_reported") else None,
                 "duration_ms": int((utc(turn.updated_at) - utc(turn.created_at)).total_seconds() * 1000)},
        completed_at=turn.updated_at,
    ), discoveries


def _build_task(
    db: Session, task: AgentTask, identity: str,
) -> tuple[ExperienceEpisode | None, list[tuple[AgentTaskEvent, str | None]]]:
    reader = TaskReadRepository(db)
    outcomes = {"succeeded": "agent_reported_success", "partial": "partial", "failed": "failed"}
    outcome = outcomes.get(task.status)
    if outcome is None or task.completed_at is None or not reader.task_visible(task):
        return None, []
    actions = reader.actions(task.id, limit=100_000)
    events = reader.signals(task.id, ["tool_search", "tool_describe", "task_outcome_verified",
                                      "experience_used", "task_completed"], limit=100_000)
    discoveries = [event for event in events if event.event_type in {"tool_search", "tool_describe"}]
    searches = [event for event in discoveries if event.event_type == "tool_search" and "matched" in event.payload
                and event.payload.get("experience_classification") != "discovery_temporary_failure"]
    proof = next((event for event in events if event.event_type == "task_outcome_verified"), None)
    if outcome == "agent_reported_success" and proof is not None:
        outcome = "verified_success"
    runs = reader.filter_children("agent_run", db.query(AgentRun).filter_by(task_id=task.id).all())
    actual = [action for action in actions if action.status in TOOL_EXECUTED]
    usage = task.usage or {}
    report_usage = (task.report or {}).get("report_usage") or {}
    token_usage = sum(part.get(key, 0) for part in (usage, report_usage)
                      for key in ("prompt_tokens", "completion_tokens"))
    return ExperienceEpisode(
        id=identity, kind="task", source_id=task.id, thread_id=task.thread_id,
        parent_id=stable_id("turn", task.source_turn_record_id) if task.source_turn_record_id else None,
        task_type=task.task_type, family=family_for(task.goal, task.task_type), outcome=outcome,
        refs={"run_ids": [run.id for run in runs], "action_ids": [action.id for action in actions],
              "task_event_ids": [event.id for event in events], "verification_event_id": proof.id if proof else None,
              "evidence_ids": [item.id for item in reader.evidence(task.id)],
              "artifact_ids": [item.id for item in reader.artifacts(task.id, limit=100_000)]},
        uses=merge_uses(*(event.payload.get("uses") or [] for event in events
                          if event.event_type in {"tool_search", "experience_used"})),
        metrics={"actions": len(actual), "tool_calls": len(actual),
                 "tool_failures": sum(action.status == "failed" for action in actual),
                 "searches": len(searches), "search_misses": sum(not event.payload["matched"] for event in searches),
                 "tokens": token_usage or None,
                 "duration_ms": int((utc(task.completed_at) - utc(task.created_at)).total_seconds() * 1000)},
        completed_at=task.completed_at,
    ), [(event, event.payload.get("experience_classification")) for event in discoveries]


def discovery_classification(
    db: Session, event_type: str, payload: dict[str, Any],
) -> str | None:
    if event_type == "tool_search" and "matched" in payload:
        if not payload["matched"]:
            if payload.get("status") not in {"ready", "disabled", "empty_catalog"}:
                return "discovery_temporary_failure"
            return "search_no_results"
        if payload.get("any_in_scope") is False or payload.get("reason") == "search_results_out_of_scope":
            return "capability_out_of_scope"
    if event_type == "tool_describe":
        if payload.get("found"):
            return None if payload.get("in_scope") else "capability_out_of_scope"
        capability_id = payload.get("capability_id")
        return "capability_unavailable" if known_capability(db, capability_id) else "capability_missing_from_catalog"
    return None



def known_capability(db: Session, capability_id: str | None) -> bool:
    from aiive.tools.registry import get_tool_registry

    if not capability_id:
        return False
    if (get_tool_registry().get(capability_id) is not None
            or db.query(Capability.id).filter_by(capability_id=capability_id).first() is not None):
        return True
    return any(item.get("name") == capability_id
               for (capabilities,) in db.query(DesktopNode.capabilities)
               for item in capabilities or [])
