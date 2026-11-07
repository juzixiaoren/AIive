"""Bounded visible Memory pagination, shared by ordinary readers.

State/scope SQL belongs to the caller's purpose; Forget is always applied here.
Raw maintenance/cascade queries must not use this content-facing helper.
"""
from sqlalchemy.orm import Query, Session

from aiive.db.models import MemoryRecord
from aiive.forget.visibility_service import ForgetVisibilityService


def visible_memory_page(db: Session, query: Query[MemoryRecord], limit: int, *, scan_limit: int = 4096) -> list[MemoryRecord]:
    query = query.limit(None).offset(None).order_by(MemoryRecord.id.asc())
    visible = []
    offset = 0
    batch_size = min(max(limit, 32), 256)
    while len(visible) < limit and offset < scan_limit:
        rows = query.offset(offset).limit(min(batch_size, scan_limit - offset)).all()
        if not rows:
            break
        offset += len(rows)
        allowed = set(ForgetVisibilityService.filter_memory_ids(db, [r.id for r in rows]))
        visible.extend(r for r in rows if r.id in allowed and r.lifecycle_state != "forgotten")
    return visible[:limit]
