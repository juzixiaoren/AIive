"""Durable dependency proofs for task-derived text; no content is stored here."""
from collections.abc import Collection, Mapping

from sqlalchemy.orm import Session
from aiive.db.models import SegmentSummary
from aiive.db.forget_models import ContentProvenanceRef
from aiive.worker.outbox_payload_contract import parse_reference_ids

REFERENCE_KEYS = {
    "trigger_event_id": "event", "event_ids": "event",
    "event_id": "event", "source_event_id": "event", "source_event_ids": "event",
    "memory_id": "memory_record", "source_memory_id": "memory_record", "memory_ids": "memory_record",
    "turn_record_id": "turn_record", "source_turn_record_id": "turn_record",
    "turn_id": "turn_record", "source_turn_id": "turn_record",
}


def collect_sources(value: object, refs: set[tuple[str, str]]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in REFERENCE_KEYS and item is not None:
                ids = set()
                if not parse_reference_ids(item, ids):
                    raise ValueError("invalid_source_reference")
                refs.update((REFERENCE_KEYS[key], token) for token in ids)
            elif isinstance(item, (dict, list)):
                collect_sources(item, refs)
    elif isinstance(value, list):
        for item in value:
            collect_sources(item, refs)


def persist_sources(db: Session, owner_type: str, owner_id: str, *payloads: object) -> None:
    refs = set()
    try:
        for payload in payloads:
            collect_sources(payload, refs)
    except ValueError:
        refs = {("unresolved", "invalid_source_reference")}
    existing = {(r.source_type, r.source_id) for r in db.query(ContentProvenanceRef).filter_by(
        owner_type=owner_type, owner_id=owner_id).all()}
    for kind, token in refs - existing:
        db.add(ContentProvenanceRef(owner_type=owner_type, owner_id=owner_id, source_type=kind, source_id=token))


def references_visible(db: Session, grouped: Mapping[str, Collection[str]]) -> bool:
    if any(kind not in {"event", "memory_record", "turn_record"} for kind in grouped):
        return False
    # Reuse the worker contract, including Turn -> child Event closure and missing metadata.
    from aiive.worker.job_source_guard import JobSources, blocked_source_reason
    from aiive.worker.outbox_dto import ClaimedJob
    sources = JobSources(events={i: None for i in grouped.get("event", [])},
                         memories={i: None for i in grouped.get("memory_record", [])},
                         turns={i: None for i in grouped.get("turn_record", [])})
    claimed = ClaimedJob("content", "content_read", {}, None, 0, 1, "", 1, "")
    return not blocked_source_reason(db, claimed, sources)


def sources_visible(db: Session, owner_type: str, owner_id: str) -> bool:
    refs = db.query(ContentProvenanceRef).filter_by(owner_type=owner_type, owner_id=owner_id).all()
    if not refs:
        return False
    grouped = {}
    for ref in refs:
        grouped.setdefault(ref.source_type, set()).add(ref.source_id)
    return references_visible(db, grouped)


def summary_sources_visible(db: Session, summary: SegmentSummary) -> bool:
    """A sealing snapshot must prove all its declared and durable dependencies."""
    dependencies = set()
    try:
        collect_sources({"source_event_ids": summary.source_event_ids,
                         "source_turn_id": summary.source_turn_ids}, dependencies)
    except ValueError:
        return False
    refs = db.query(ContentProvenanceRef).filter_by(owner_type="segment_summary", owner_id=summary.id).all()
    dependencies.update((r.source_type, r.source_id) for r in refs)
    grouped = {}
    for kind, token in dependencies:
        grouped.setdefault(kind, set()).add(token)
    return bool(dependencies) and references_visible(db, grouped)
