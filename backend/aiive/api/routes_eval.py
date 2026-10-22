"""Read-only query API for self-evolution safety-gate evaluation results.

Results are produced by the gate (contracts + replay eval) and persisted as JSON
under <repo>/runtime/eval_runs/ (see aiive.eval.persistence). This router only
reads them back; it never triggers or mutates an evaluation.

Access control: same guard as the other developer diagnostics routers
(routes_debug / routes_outbox) — loopback + diagnostics switch.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from aiive.api.developer_security import require_local_developer
from aiive.eval import persistence

router = APIRouter(prefix="/api/eval", dependencies=[Depends(require_local_developer)])


@router.get("/runs")
def list_eval_runs():
    """List historical gate runs, most-recent first."""
    return {"runs": persistence.load_runs()}


@router.get("/runs/latest")
def latest_eval_run():
    """Return the most recent gate run, or an empty payload if none exist."""
    run = persistence.latest()
    if run is None:
        return {"run": None}
    return {"run": run}
