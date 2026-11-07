"""Read-only consistency summary. Reports IDs/reasons, never forgotten content."""
from sqlalchemy.orm import Session

from aiive.db.models import MemoryRecord, GraphRelation
from aiive.db.forget_models import ContentProvenanceRef
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
