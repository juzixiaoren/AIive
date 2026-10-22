"""Safety gate that promotes only when kernel contracts and the replay eval pass.

Fail-closed: any critical contract failure, replay crash/failure, or candidate-eval
failure (include a non-zero/garbled candidate subprocess) blocks promotion. The gate never
silently passes.

Severity semantics (see eval/models.py::ContractResult):
  - severity="critical" (default): failure blocks promotion.
  - severity="warning": failure is recorded in notes but does NOT block (diagnostics only).
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


def evaluate_promotion_gate(
    candidate_slot_root: Path | str | None,
    persist: bool = True,
    gate: SafetyGate | None = None,
) -> GateResult:
    """晋升安全门的统一入口（fail-closed）：PromoteRollback 与 ReleaseManager 共用。

    Args:
        candidate_slot_root: 非活跃槽位根目录，其下 ``app/`` 为候选代码树；
            为 None 或 app/ 不存在时跳过候选子进程评测（契约 + 重放仍必跑）。
        persist: 是否将闸门结果落盘到 runtime/eval_runs/。
        gate: 可注入的 SafetyGate 实例（测试替身）；默认构造真实闸门。

    Returns:
        GateResult。任何异常都转换为 ok=False 的结果，绝不向上抛异常、
        也绝不静默放行——调用方只需检查 ``.ok``。
    """
    candidate_app_dir: str | None = None
    if candidate_slot_root is not None:
        candidate_app = Path(candidate_slot_root) / "app"
        if candidate_app.exists():
            candidate_app_dir = str(candidate_app)
    try:
        return (gate or SafetyGate()).evaluate(
            candidate_app_dir=candidate_app_dir, persist=persist,
        )
    except Exception as exc:  # fail-closed: 任何异常都不得放行晋升
        logger.exception("safety gate raised during promotion evaluation")
        return GateResult(
            ok=False,
            error="Safety gate errored during promotion (blocked): " + str(exc),
        )


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
            notes.append("candidate eval attempted (subprocess)")
        else:
            notes.append("candidate eval skipped: no candidate app dir")

        # 只有 critical 失败阻断；warning 失败仅记录（诊断用途，不充当刹车）。
        critical_failed = [c for c in contracts if not c.ok and c.severity != "warning"]
        warning_failed = [c for c in contracts if not c.ok and c.severity == "warning"]
        if warning_failed:
            notes.append(
                "warning contract(s) failed (non-blocking): "
                + ", ".join(c.name for c in warning_failed)
            )
        contract_ok = not critical_failed
        replay_ok = replay.ok
        candidate_ok = candidate is None or candidate.ok
        ok = contract_ok and replay_ok and candidate_ok

        error = ""
        if not contract_ok:
            error = "kernel contract check(s) failed: " + ", ".join(c.name for c in critical_failed)
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

    def _run_candidate(self, candidate_app_dir: str) -> EvalRunResult:
        """Run the candidate slot's own eval as a subprocess (fail-closed).

        候选 app 目录存在但没有 aiive/eval 包（被删除或从未存在）一律判定失败：
        本功能上线后所有槽位都应携带评测包，缺失即视为候选被破坏，fail-closed。
        """
        import json

        backend = Path(candidate_app_dir) / "backend"
        if not (backend / "aiive" / "eval").exists():
            return EvalRunResult(
                error="candidate has no aiive/eval package: " + str(backend / "aiive" / "eval"),
            )

        with tempfile.TemporaryDirectory(prefix="eval_cand_") as tmp_dir:
            out_file = Path(tmp_dir) / "result.json"
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
                return EvalRunResult(
                    error="candidate subprocess exit=" + str(proc.returncode) + " :: " + tail,
                )

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
