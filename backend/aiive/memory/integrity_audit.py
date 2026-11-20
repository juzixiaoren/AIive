"""Memory consistency and provenance maintenance. Reports identifiers and reasons."""
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, load_only

from aiive.context.run_context import load_user_request_event
from aiive.db.models import Event, GraphRelation, MemoryEvidence, MemoryProposal, MemoryRecord, TurnRecord
from aiive.db.forget_models import ContentProvenanceRef
from aiive.forget.concurrency import acquire_forget_exclusive_lock
from aiive.forget.visibility_service import ForgetVisibilityService


def audit_memory_integrity(db: Session, *, batch_size: int = 200) -> dict[str, int | list[dict[str, str]]]:
    findings = []
    last_id = ""
    scanned = 0
    while True:
        records = db.query(MemoryRecord).filter(MemoryRecord.id > last_id).order_by(MemoryRecord.id).limit(batch_size).all()
        if not records:
            break
        last_id = records[-1].id
        scanned += len(records)
        blocked = ForgetVisibilityService.blocked_target_ids(db, "memory_record", [(r.id, r.created_at) for r in records])
        for record in records:
            if record.id in blocked and record.lifecycle_state != "forgotten":
                findings.append({"id": record.id, "reason": "source_blocked_or_unresolved"})
            if record.lifecycle_state == "candidate" and record.validity_state != "valid":
                findings.append({"id": record.id, "reason": "invalid_candidate"})
    refs = db.query(ContentProvenanceRef).filter_by(owner_type="memory_record").all()
    owners = {r[0] for r in db.query(MemoryRecord.id).all()}
    for ref in refs:
        if ref.owner_id not in owners:
            findings.append({"id": ref.id, "reason": "orphan_memory_provenance"})
    last_relation = ""
    while True:
        relations = db.query(GraphRelation).filter(GraphRelation.id > last_relation).order_by(GraphRelation.id).limit(batch_size).all()
        if not relations:
            break
        last_relation = relations[-1].id
        ids = {r.source_memory_id for r in relations}
        allowed = set(ForgetVisibilityService.filter_memory_ids(db, list(ids)))
        for relation in relations:
            if relation.status == "active" and relation.source_memory_id not in allowed:
                findings.append({"id": relation.id, "reason": "blocked_active_graph_source"})
    return {"scanned": scanned, "findings": findings}


@dataclass
class _DanglingSource:
    record_ids: set[str] = field(default_factory=set)
    evidence: list[MemoryEvidence] = field(default_factory=list)
    proposals: list[MemoryProposal] = field(default_factory=list)
    refs: list[ContentProvenanceRef] = field(default_factory=list)

    def memory_ids(self) -> set[str]:
        return (self.record_ids | {row.memory_id for row in self.evidence}
                | {row.owner_id for row in self.refs}
                | {row.final_memory_id for row in self.proposals if row.final_memory_id})


def _proposal_event_ids(proposal: MemoryProposal) -> set[str]:
    declared = proposal.source_event_ids if isinstance(proposal.source_event_ids, list) else []
    evidence: list[Any] = proposal.evidence if isinstance(proposal.evidence, list) else []
    return {value for value in [*declared, *(row.get("source_event_id") for row in evidence if isinstance(row, dict))]
            if isinstance(value, str) and value}


def _replacement_event(
    db: Session, missing_id: str, group: _DanglingSource, memory_ids: set[str],
) -> tuple[str | None, str]:
    turn_ids = {row.source_turn_record_id for row in group.proposals}
    if len(turn_ids) != 1 or None in turn_ids or "" in turn_ids:
        return None, "unique_source_turn_required"

    # Direct writes have exactly one user source. A missing source in a mixed
    # proposal might instead be an assistant event or inherited merge evidence.
    if any(row.source_event_ids != [missing_id] or row.extractor_name != "remember_or_update_tool"
           or _proposal_event_ids(row) != {missing_id} for row in group.proposals):
        return None, "user_request_source_unproven"
    if memory_ids - {row.final_memory_id for row in group.proposals}:
        return None, "memory_owner_without_proof"

    turn = db.get(TurnRecord, next(iter(turn_ids)))
    if turn is None or any(row.source_turn_id and row.source_turn_id != turn.turn_id for row in group.proposals):
        return None, "source_turn_mismatch"
    event = load_user_request_event(db, turn)
    if event is None:
        return None, "user_request_event_required"

    visibility = ForgetVisibilityService
    if (visibility.blocked_target_ids(db, "event", [(missing_id, None), (event.id, event.created_at)])
            or visibility.blocked_target_ids(db, "turn_record", [(turn.id, turn.created_at)])
            or visibility.is_tombstone_blocked_reingestion(db, source_event_id=missing_id)
            or visibility.is_tombstone_blocked_reingestion(db, source_event_id=event.id, source_turn_record_id=turn.id)):
        return None, "source_forget_blocked"

    # Offline maintenance only reads owner metadata, including records hidden from normal readers.
    owners = db.query(MemoryRecord.id, MemoryRecord.created_at, MemoryRecord.lifecycle_state).filter(
        MemoryRecord.id.in_(memory_ids),
    ).all()
    if (len(owners) != len(memory_ids) or any(row.lifecycle_state == "forgotten" for row in owners)
            or visibility.blocked_target_ids(db, "memory_record", [(row.id, row.created_at) for row in owners],
                                             _expand_provenance=False)):
        return None, "memory_owner_unavailable"
    return event.id, "verified_user_request_event"


def audit_or_repair_memory_sources(db: Session, *, apply: bool = False) -> list[dict[str, Any]]:
    """Scan dangling Event references and optionally repair proven legacy writes atomically."""
    if apply:
        # Serialize repair with MemoryWrite and Forget Phase A, including all visibility checks.
        acquire_forget_exclusive_lock(db)
    groups: dict[str, _DanglingSource] = defaultdict(_DanglingSource)
    for record_id, source_id in db.query(MemoryRecord.id, MemoryRecord.source_event_id).outerjoin(
        Event, Event.id == MemoryRecord.source_event_id,
    ).filter(MemoryRecord.source_event_id.is_not(None), Event.id.is_(None)).all():
        if source_id is not None:
            groups[source_id].record_ids.add(record_id)
    for row in db.query(MemoryEvidence).outerjoin(Event, Event.id == MemoryEvidence.source_event_id).options(
        load_only(MemoryEvidence.id, MemoryEvidence.memory_id, MemoryEvidence.source_event_id),
    ).filter(MemoryEvidence.source_event_id.is_not(None), Event.id.is_(None)).all():
        if row.source_event_id is not None:
            groups[row.source_event_id].evidence.append(row)
    for ref in db.query(ContentProvenanceRef).outerjoin(Event, Event.id == ContentProvenanceRef.source_id).filter(
        ContentProvenanceRef.owner_type == "memory_record", ContentProvenanceRef.source_type == "event", Event.id.is_(None),
    ).all():
        groups[ref.source_id].refs.append(ref)

    proposals = select(MemoryProposal).options(load_only(
        MemoryProposal.id, MemoryProposal.final_memory_id, MemoryProposal.source_turn_record_id,
        MemoryProposal.source_turn_id, MemoryProposal.source_event_ids, MemoryProposal.evidence, MemoryProposal.extractor_name,
    )).execution_options(yield_per=200)
    for batch in db.scalars(proposals).partitions(200):
        sources = [(row, _proposal_event_ids(row)) for row in batch]
        ids = set().union(*(ids for _, ids in sources))
        existing = {row[0] for row in db.query(Event.id).filter(Event.id.in_(ids)).all()}
        for row, event_ids in sources:
            for missing_id in event_ids - existing:
                groups[missing_id].proposals.append(row)

    findings = []
    for missing_id, group in sorted(groups.items()):
        memory_ids = group.memory_ids()
        replacement, reason = _replacement_event(db, missing_id, group, memory_ids)
        if apply and replacement is not None:
            _repair_source(db, group, memory_ids, missing_id, replacement)
        findings.append({
            "memory_ids": sorted(memory_ids),
            "proposal_ids": sorted(row.id for row in group.proposals),
            "evidence_ids": sorted(row.id for row in group.evidence),
            "provenance_ids": sorted(row.id for row in group.refs),
            "status": ("repaired" if apply else "repairable") if replacement else "unresolved",
            "reason": reason,
        })
    return findings


def _repair_source(db: Session, group: _DanglingSource, memory_ids: set[str], old: str, new: str) -> None:
    if group.record_ids:
        db.execute(update(MemoryRecord).where(MemoryRecord.id.in_(group.record_ids)).values(
            source_event_id=new, updated_at=MemoryRecord.updated_at,
        ))
    for evidence in group.evidence:
        evidence.source_event_id = new
    for proposal in group.proposals:
        proposal.source_event_ids = [new]
        evidence: Any = proposal.evidence
        if isinstance(evidence, list):
            proposal.evidence = [
                {**row, "source_event_id": new} if isinstance(row, dict) and row.get("source_event_id") == old else row
                for row in evidence
            ]

    existing = {row[0] for row in db.query(ContentProvenanceRef.owner_id).filter(
        ContentProvenanceRef.owner_type == "memory_record", ContentProvenanceRef.source_type == "event",
        ContentProvenanceRef.owner_id.in_(memory_ids), ContentProvenanceRef.source_id == new,
    ).all()}
    for ref in group.refs:
        if ref.owner_id in existing:
            db.delete(ref)
        else:
            ref.source_id = new
            existing.add(ref.owner_id)
    for memory_id in memory_ids - existing:
        db.add(ContentProvenanceRef(owner_type="memory_record", owner_id=memory_id, source_type="event", source_id=new))
    db.flush()


def main() -> None:
    import argparse
    import json

    from aiive.db.base import SessionLocal

    parser = argparse.ArgumentParser(description="Audit dangling memory Event references")
    parser.add_argument("--apply", action="store_true", help="repair verified visible sources in one transaction")
    args = parser.parse_args()
    try:
        with SessionLocal() as db, db.begin():
            findings = audit_or_repair_memory_sources(db, apply=args.apply)
    except SQLAlchemyError as exc:
        parser.exit(1, f"Memory source audit failed ({type(exc).__name__}).\n")
    print(json.dumps({"findings": findings}, ensure_ascii=False))


if __name__ == "__main__":
    main()
