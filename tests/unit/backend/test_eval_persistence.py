"""Persistence of gate / eval results (file-state, read-only query)."""
from pathlib import Path

from aiive.eval import persistence
from aiive.eval.models import ContractResult, EvalRunResult, GateResult


def test_save_and_load_roundtrip(tmp_path):
    res = GateResult(
        ok=True,
        contracts=[ContractResult("memory_single_source_of_truth", True, "critical")],
        replay=EvalRunResult(passed=24, failed=0),
        candidate_eval=None,
    )
    path = persistence.save_gate_result(res, directory=str(tmp_path))
    import os
    assert os.path.exists(path)

    runs = persistence.load_runs(str(tmp_path))
    assert len(runs) == 1
    assert runs[0]["ok"] is True
    assert runs[0]["replay"]["passed"] == 24
    assert runs[0]["contracts"][0]["name"] == "memory_single_source_of_truth"

    latest = persistence.latest(str(tmp_path))
    assert latest is not None
    assert latest["ok"] is True


def test_load_empty(tmp_path):
    assert persistence.load_runs(str(tmp_path)) == []
    assert persistence.latest(str(tmp_path)) is None


def test_default_dir_prefers_env_override(tmp_path, monkeypatch):
    """AIIVE_EVAL_RUNS_DIR 显式覆盖优先，save/latest 无参调用也走该目录。"""
    custom = tmp_path / "custom_runs"
    monkeypatch.setenv("AIIVE_EVAL_RUNS_DIR", str(custom))

    assert persistence._default_dir() == custom
    res = GateResult(ok=True, contracts=[], replay=EvalRunResult(passed=3, failed=0))
    path = persistence.save_gate_result(res)
    assert Path(path).parent == custom
    assert persistence.latest() is not None
    assert persistence.latest()["ok"] is True


def test_default_dir_detects_repo_root(monkeypatch):
    """无覆盖时从源码布局向上定位仓库根（含 alembic.ini），落到 runtime/eval_runs。"""
    monkeypatch.delenv("AIIVE_EVAL_RUNS_DIR", raising=False)
    resolved = persistence._default_dir()
    assert resolved.name == "eval_runs"
    assert resolved.parent.name == "runtime"
    assert (resolved.parent.parent / "alembic.ini").is_file()
