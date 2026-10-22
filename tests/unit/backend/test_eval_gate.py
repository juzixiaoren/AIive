"""Safety gate tests: pass on clean system, fail-closed on violations, and promote integration."""
from aiive.eval.gate import SafetyGate
from aiive.eval.models import ContractResult, EvalRunResult, GateResult
from aiive.eval.replay import run_replay_isolated
from aiive.memory.memory_store import MemoryStore
from aiive.memory.memory_types import (
    EvidenceItem,
    LifecycleState,
    MemoryProposal,
    TrustLevel,
)
from aiive.selfdev.promote_rollback import PromoteRollback
from aiive.supervisor.slot_manager import SlotManager


def _make_proposal(key, content):
    p = MemoryProposal(
        memory_type="user_profile", canonical_key=key, content=content,
        evidence=[EvidenceItem(source_type="user_message", trust_level=TrustLevel.TRUSTED.value)],
    )
    p.compute_request_idempotency()
    return p


def test_gate_ok_on_clean_system():
    res = SafetyGate().evaluate()
    assert res.ok is True, res.error
    assert res.replay is not None and res.replay.ok
    assert res.replay.passed == len(res.replay.cases)


def test_gate_fails_on_contract_violation(db_session):
    # Inject a duplicate active memory into the LIVE session passed to the gate.
    store = MemoryStore(db_session)
    store.create_record(_make_proposal("user.eval.dup", "v1"), lifecycle_state=LifecycleState.ACTIVE.value)
    store.create_record(_make_proposal("user.eval.dup", "v2"), lifecycle_state=LifecycleState.ACTIVE.value)
    db_session.flush()

    res = SafetyGate().evaluate(live_session=db_session)
    assert res.ok is False
    assert res.error  # must explain why
    violated = [c for c in res.contracts if not c.ok]
    assert violated, "expected at least one failed contract"
    assert any(c.name == "memory_single_source_of_truth" for c in violated)


def test_gate_candidate_subprocess_failure(tmp_path):
    # A candidate whose aiive package is broken must fail the gate (fail-closed).
    cand = tmp_path / "candidate" / "backend" / "aiive"
    (cand / "eval").mkdir(parents=True)
    # Regular aiive package at the top so it shadows the real one, and it is broken.
    (cand / "__init__.py").write_text("raise RuntimeError('broken candidate')", encoding="utf-8")
    (cand / "eval" / "__init__.py").write_text("", encoding="utf-8")
    res = SafetyGate().evaluate(candidate_app_dir=str(tmp_path / "candidate"))
    assert res.ok is False
    assert res.candidate_eval is not None
    assert res.candidate_eval.ok is False


def test_promote_blocked_when_gate_fails(monkeypatch, tmp_path):
    mgr = SlotManager(base_dir=tmp_path / "slots")
    mgr.init_slots()
    pr = PromoteRollback(manager=mgr)

    def _fail(self, **kwargs):
        return GateResult(
            ok=False, error="simulated contract failure",
            contracts=[ContractResult("memory_single_source_of_truth", False, "critical", "sim")],
            replay=EvalRunResult(passed=0, failed=1),
        )
    monkeypatch.setattr("aiive.eval.gate.SafetyGate.evaluate", _fail)

    r = pr.promote(run_gate=True)
    assert r["ok"] is False
    assert "Safety gate failed" in r["error"]
    # slot must NOT have switched
    assert mgr.get_active_slot() == "A"


def test_promote_proceeds_when_gate_ok(monkeypatch, tmp_path):
    mgr = SlotManager(base_dir=tmp_path / "slots")
    mgr.init_slots()
    pr = PromoteRollback(manager=mgr)

    def _ok(self, **kwargs):
        rr = run_replay_isolated()
        return GateResult(ok=True, contracts=[], replay=rr, candidate_eval=None)

    monkeypatch.setattr("aiive.eval.gate.SafetyGate.evaluate", _ok)

    r = pr.promote(run_gate=True)
    assert r["ok"] is True
    assert mgr.get_active_slot() == "B"
    assert r["new_active"] == "B"
