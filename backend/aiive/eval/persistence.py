"""Persistence for gate / eval results.

Reuses the project's file-state convention (slot manifests / release state): results
are written as JSON under <repo>/runtime/eval_runs/. A read-only API reads them back.
No new DB tables, no UI dependency.

Directory resolution order (first hit wins):
  1. AIIVE_EVAL_RUNS_DIR environment variable (explicit override, e.g. Docker volume);
  2. repo root detected by walking upward from this file (source layout:
     <repo>/backend/aiive/eval/persistence.py -> <repo>);
  3. repo root detected from the current working directory (wheel/site-packages
     layout, e.g. the Docker backend container whose WORKDIR holds alembic.ini);
  4. legacy fallback: parents[3] of this file.

A repo root is a directory containing ``alembic.ini`` (or both ``pyproject.toml``
and ``backend/``), matching the markers copied into deployment images.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from aiive.eval.models import GateResult

_ENV_OVERRIDE = "AIIVE_EVAL_RUNS_DIR"


def _is_repo_root(path: Path) -> bool:
    if (path / "alembic.ini").is_file():
        return True
    return (path / "pyproject.toml").is_file() and (path / "backend").is_dir()


def _find_repo_root() -> Path | None:
    for origin in (Path(__file__).resolve(), Path.cwd().resolve()):
        for parent in (origin, *origin.parents):
            if _is_repo_root(parent):
                return parent
    return None


def _default_dir() -> Path:
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        return Path(override)
    root = _find_repo_root()
    if root is not None:
        return root / "runtime" / "eval_runs"
    # 源码布局的历史行为；安装布局下找不到仓库标记时保持兼容
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


def load_runs_with_mtime(
    directory: str | Path | None = None,
) -> list[tuple[dict[str, object], float]]:
    """同 load_runs，但同时给出文件修改时间（证据采集需要观察时间）。"""
    directory = Path(directory) if directory else _default_dir()
    if not directory.exists():
        return []
    out: list[tuple[dict[str, object], float]] = []
    for p in sorted(directory.glob("eval_*.json"), reverse=True):
        try:
            summary = json.loads(p.read_text(encoding="utf-8"))
            mtime = p.stat().st_mtime
        except Exception:  # noqa: BLE001, S112
            continue
        out.append((summary, mtime))
    return out


def latest(directory: str | Path | None = None) -> dict[str, object] | None:
    runs = load_runs(directory)
    return runs[0] if runs else None
