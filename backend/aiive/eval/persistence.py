"""Persistence for gate / eval results.

Reuses the project's file-state convention (slot manifests / release state): results
are written as JSON under <repo>/runtime/eval_runs/. A read-only API reads them back.
No new DB tables, no UI dependency.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from aiive.eval.models import GateResult


def _default_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "runtime" / "eval_runs"


def save_gate_result(result: GateResult, directory: str | Path | None = None) -> str:
    directory = Path(directory) if directory else _default_dir()
    directory.mkdir(parents=True, exist_ok=True)
    base = "eval_" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = directory / (base + ".json")
    n = 0
    while path.exists():
        n += 1
        path = directory / (base + "_" + str(n) + ".json")
    path.write_text(
        json.dumps(result.to_summary(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return str(path)


def load_runs(directory: str | Path | None = None) -> list[dict[str, object]]:
    directory = Path(directory) if directory else _default_dir()
    if not directory.exists():
        return []
    out = []
    for p in sorted(directory.glob("eval_*.json"), reverse=True):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001, S112
            continue
    return out


def latest(directory: str | Path | None = None) -> dict[str, object] | None:
    runs = load_runs(directory)
    return runs[0] if runs else None
