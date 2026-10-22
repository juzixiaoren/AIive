"""Read-only query API for self-evolution safety-gate evaluation results.

Results are produced by the gate (contracts + replay eval) and persisted as JSON
under <repo>/runtime/eval_runs/ (see aiive.eval.persistence). This router only
reads them back; it never triggers or mutates an evaluation.
"""
from __future__ import annotations

from fastapi import APIRouter

from aiive.eval import persistence

router = APIRouter(prefix="/api/eval")


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
