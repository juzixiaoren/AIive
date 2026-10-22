"""Safety gate that promotes only when kernel contracts and the replay eval pass.

Fail-closed: any contract failure, replay crash/failure, or candidate-eval failure
(include a non-zero/garbled candidate subprocess) blocks promotion. The gate never
silently passes.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from aiive.eval.contracts import run_contract_checks
from aiive.eval.models import EvalCaseResult, EvalRunResult, GateResult, StepResult
from aiive.eval.replay import isolated_eval_session, run_replay_isolated

logger = logging.getLogger(__name__)


class SafetyGate:
    """Runs kernel contract checks + replay eval (+ optional candidate eval)."""

    def __init__(self, persist_dir: str | None = None):
        self._persist_dir: str | None = persist_dir

    def evaluate(
        self,
        live_session: Session | None = None,
        candidate_app_dir: str | None = None,
        persist: bool = False,
    ) -> GateResult:
        notes: list[str] = []

        # 1) kernel contracts: live session if caller provides one, else isolated.
        if live_session is not None:
            contracts = run_contract_checks(live_session)
            notes.append("contracts evaluated against the live session")
        else:
            with isolated_eval_session() as s:
                contracts = run_contract_checks(s)
            notes.append("contracts evaluated against an isolated session (no live session)")

        # 2) replay eval: always isolated (it writes data, must not touch live DB).
        replay = run_replay_isolated()

        # 3) candidate eval: run the candidate's own eval as a subprocess so the
        #    candidate's code is exercised, not the currently-running copy.
        candidate = None
        if candidate_app_dir is not None and Path(candidate_app_dir).exists():
            candidate = self._run_candidate(candidate_app_dir)
            if candidate is None:
                notes.append("candidate eval skipped: candidate has no eval package")
        else:
            notes.append("candidate eval skipped: no candidate app dir")

        contract_ok = all(c.ok for c in contracts)
        replay_ok = replay.ok
        candidate_ok = candidate is None or candidate.ok
        ok = contract_ok and replay_ok and candidate_ok

        error = ""
        if not contract_ok:
            failed = [c.name for c in contracts if not c.ok]
            error = "kernel contract check(s) failed: " + ", ".join(failed)
        elif not replay_ok:
            error = "replay eval failed: " + (replay.error or "one or more scenarios failed")
        elif candidate is not None and not candidate.ok:
            error = "candidate eval failed: " + (candidate.error or "scenario failures")

        result = GateResult(
            ok=ok, contracts=contracts, replay=replay,
            candidate_eval=candidate, error=error, notes=notes,
        )
        if persist:
            try:
                from aiive.eval import persistence
                persistence.save_gate_result(result, self._persist_dir)
            except Exception:
                logger.exception("failed to persist gate result")
        return result


    def _run_candidate(self, candidate_app_dir: str) -> EvalRunResult | None:
        """Run the candidate slot's own eval as a subprocess (fail-closed)."""
        import json

        backend = Path(candidate_app_dir) / "backend"
        if not (backend / "aiive" / "eval").exists():
            return None

        out_file = Path(tempfile.mkdtemp(prefix="eval_cand_")) / "result.json"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(backend) + os.pathsep + env.get("PYTHONPATH", "")
        env["AIIVE_MEMORY_VECTOR_ENABLED"] = "false"
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "aiive.eval", "run", "--json", "--out", str(out_file)],
                cwd=str(backend), env=env, capture_output=True, text=True,
                timeout=600, check=False,
            )
        except Exception as exc:  # noqa: BLE001 - fail-closed gate
            return EvalRunResult(error="candidate subprocess error: " + str(exc))

        if proc.returncode != 0 or not out_file.exists():
            tail = (proc.stderr or proc.stdout or "")[-1500:]
            return EvalRunResult(error="candidate subprocess exit=" + str(proc.returncode) + " :: " + tail)

        try:
            data = json.loads(out_file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - fail-closed gate
            return EvalRunResult(error="candidate result unparseable: " + str(exc))

        cases = [
            EvalCaseResult(
                scenario_id=c.get("scenario_id", ""),
                description=c.get("description", ""),
                ok=c.get("ok", False),
                steps_total=c.get("steps_total", 0),
                steps_passed=c.get("steps_passed", 0),
                error=c.get("error", ""),
                assertions=[
                    StepResult(ok=a.get("ok", False), message=a.get("message", ""))
                    for a in c.get("assertions", [])
                ],
            )
            for c in data.get("cases", [])
        ]
        return EvalRunResult(
            passed=data.get("passed", 0),
            failed=data.get("failed", 0),
            error=data.get("error", ""),
            cases=cases,
        )

