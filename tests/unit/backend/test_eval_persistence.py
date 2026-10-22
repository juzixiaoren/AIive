"""Persistence of gate / eval results (file-state, read-only query)."""
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
