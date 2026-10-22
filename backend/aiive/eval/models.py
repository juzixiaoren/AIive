"""Self-evolution safety gate: data models for contracts, replay eval, and the gate.

These dataclasses are plain and dependency-free so they can be serialized to
JSON for persistence and the read-only query API.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContractResult:
    """Result of a single kernel contract check."""

    name: str
    ok: bool
    severity: str = "critical"  # "critical" | "warning"
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepResult:
    """Result of executing a single replay step."""

    ok: bool
    message: str = ""


@dataclass
class EvalCaseResult:
    """Result of running one replay scenario."""

    scenario_id: str
    description: str
    ok: bool
    steps_total: int = 0
    steps_passed: int = 0
    error: str = ""
    assertions: list[StepResult] = field(default_factory=list)


@dataclass
class EvalRunResult:
    """Aggregate result of a full replay eval run."""

    passed: int = 0
    failed: int = 0
    error: str = ""  # set only when the whole run crashed (fail-closed)
    cases: list[EvalCaseResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        # A run is "ok" only when it completed and every case passed.
        return not self.error and self.failed == 0

    def to_summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "passed": self.passed,
            "failed": self.failed,
            "error": self.error,
            "cases": [
                {
                    "scenario_id": c.scenario_id,
                    "description": c.description,
                    "ok": c.ok,
                    "steps_total": c.steps_total,
                    "steps_passed": c.steps_passed,
                    "error": c.error,
                    "assertions": [{"ok": a.ok, "message": a.message} for a in c.assertions],
                }
                for c in self.cases
            ],
        }


@dataclass
class GateResult:
    """Aggregate result of the full safety gate (contracts + replay + candidate)."""

    ok: bool
    contracts: list[ContractResult] = field(default_factory=list)
    replay: EvalRunResult | None = None
    candidate_eval: EvalRunResult | None = None
    error: str = ""
    # Free-form notes useful for logs / API response.
    notes: list[str] = field(default_factory=list)

    def to_summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "contracts": [
                {
                    "name": c.name,
                    "ok": c.ok,
                    "severity": c.severity,
                    "detail": c.detail,
                    "evidence": c.evidence,
                }
                for c in self.contracts
            ],
            "replay": self.replay.to_summary() if self.replay else None,
            "candidate_eval": self.candidate_eval.to_summary() if self.candidate_eval else None,
            "notes": self.notes,
        }
