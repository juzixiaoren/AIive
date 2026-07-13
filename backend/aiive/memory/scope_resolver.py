"""Scope Chain resolver (V2 §九).

Builds the authoritative ScopeContext from the Runtime RunContext. The Runtime is
the only legitimate source of scope; the model may narrow the filter range but
must never expand beyond authorized scopes.

The capability scope is the only high-level scope the runtime currently tracks:
it is resolved from the `capabilities` table (state == "active"). project /
workspace / environment scopes are forwarded from RunContext when the entry point
supplies them; until then the chain degrades to thread → global, with the
extension point intact.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from aiive.context.run_context import RunContext
from aiive.memory.recall_models import ScopeContext

logger = logging.getLogger(__name__)


def build_scope_context(
    db: Session, run_ctx: RunContext | None, thread_id: str
) -> ScopeContext:
    """Construct the ScopeContext for a recall, filling capability scope from DB.

    Args:
        db: DB session used to resolve active capabilities.
        run_ctx: the current run context (may carry project/workspace/
            environment/capability scope set by the entry point).
        thread_id: fallback thread id when run_ctx is absent.
    """
    base = run_ctx.to_scope_context() if run_ctx is not None else ScopeContext(thread_id=thread_id)
    if not base.capability_ids:
        base.capability_ids = _active_capability_ids(db)
    return base


def _active_capability_ids(db: Session) -> list[str]:
    """Return ids of currently active (state == 'active') capabilities."""
    from aiive.db.models import Capability

    try:
        rows = db.query(Capability.capability_id).filter(Capability.state == "active").all()
        return [r[0] for r in rows if r[0]]
    except Exception:
        logger.exception("解析活跃 capability 失败")
        return []
