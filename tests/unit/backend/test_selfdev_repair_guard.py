"""自主修复闭环加固测试：并发锁、候选版本隔离、指纹绑定与中断恢复。

全部用例离线运行：槽位用 tmp_path 下的真实 A/B 目录，补丁执行走真实
PatchExecutor，测试运行器/闸门/晋升用替身，不访问网络、不需要 API Key。
"""

import json
import os
from pathlib import Path

import pytest
from aiive.eval.models import GateResult
from aiive.selfdev import repair_guard as guard_module
from aiive.selfdev.patch_executor import PatchExecutor
from aiive.selfdev.project_reader import ProjectContext
from aiive.selfdev.repair_guard import (
    CandidateSlotGuard,
    RepairJournal,
    RepairLock,
    apply_candidate_patch,
    finalize_candidate_patch,
    recover_interrupted,
)
from aiive.selfdev.repair_loop import SelfDevRepairLoop
from aiive.supervisor.health_probe import HealthResult
from aiive.supervisor.release_manager import ReleaseManager
from aiive.supervisor.slot_manager import (
    SlotManager,
    read_candidate_state,
    sync_app_tree,
    tree_fingerprint,
    write_candidate_state,
)

BASELINE_TEXT = "original\n"


# ── 测试替身 ──────────────────────────────────────────────────────


class ScriptedPlanner:
    """按脚本返回补丁计划，并记录调用次数。"""

    def __init__(self, plans):
        self._plans = list(plans)
        self.calls = 0

    def plan(
        self, goal, trace_id=None, *, context=None,
        previous_plan=None, failure_feedback=None,
    ):
        self.calls += 1
        if not self._plans:
            return {"operations": []}
        return self._plans.pop(0)


class StaticReader:
    """返回空上下文，避免读真实仓库。"""

    def collect_context(self, goal, *, explicit_files=None):
        return ProjectContext()


class ReportQueue:
    """按顺序产出测试报告；元素可以是报告字典或返回报告的工厂。"""

    def __init__(self, reports):
        self._reports = reports

    def run_for_changed_files(self, changed_files):
        if not self._reports:
            return _passed()
        item = self._reports.pop(0)
        return item() if callable(item) else item


class PromoteRecorder:
    """记录晋升调用次数。"""

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.result


class HealthyProbe:
    """健康检查替身：始终健康。"""

    def check(self, slot: str, slot_root: str) -> HealthResult:
        return HealthResult(healthy=True, slot=slot, message="ok")


class PassGate:
    """安全门替身：始终放行。"""

    def evaluate(self, candidate_app_dir=None, persist: bool = True) -> GateResult:
        return GateResult(ok=True, error="")


def _plan(content="patched", target="existing.py"):
    return {
        "goal_summary": "fix existing.py",
        "operations": [{
            "operation": "modify_file",
            "target_file": target,
            "content": content,
            "reason": "fix",
            "not_allowed_yet": False,
        }],
        "test_plan": "pytest",
        "requires_schema_change": False,
    }


def _passed():
    return {
        "ok": True,
        "total": 1,
        "passed": 1,
        "failed": 0,
        "results": [{"file": "tests/x.py", "result": "passed"}],
    }


def _failed(message="AssertionError: boom"):
    return {
        "ok": False,
        "total": 1,
        "passed": 0,
        "failed": 1,
        "results": [{"file": "tests/x.py", "result": "failed", "stdout": message}],
    }


# ── 夹具与装配 ────────────────────────────────────────────────────


@pytest.fixture()
def slots(tmp_path):
    """初始化 A/B 槽位，A 为活跃槽位，并放入基线代码。"""
    manager = SlotManager(base_dir=tmp_path / "slots")
    manager.init_slots()
    active = tmp_path / "slots" / "A" / "app"
    active.mkdir(parents=True, exist_ok=True)
    (active / "existing.py").write_text(BASELINE_TEXT, encoding="utf-8")
    (active / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    return manager


def _slot_root(manager, name: str) -> Path:
    return next(item.root for item in manager.list_slots() if item.name == name)


def _candidate_app(manager) -> Path:
    active = manager.get_active_slot()
    name = "B" if active == "A" else "A"
    return _slot_root(manager, name) / "app"


class Harness:
    """把闭环与临时状态目录装配在一起。"""

    def __init__(
        self,
        manager,
        tmp_path,
        *,
        plans,
        reports,
        gate=None,
        promote=None,
        max_rounds=3,
        lock=None,
        journal=None,
    ):
        self.manager = manager
        self.state_dir = tmp_path / "state"
        self.planner = ScriptedPlanner(plans)
        self.promote = promote or PromoteRecorder({"ok": True})
        self.guard = CandidateSlotGuard(manager=manager)
        self.journal = journal or RepairJournal(self.state_dir / "runs")
        self.lock = lock or RepairLock(self.state_dir / "locks" / "repair.lock")
        self.loop = SelfDevRepairLoop(
            planner=self.planner,
            reader=StaticReader(),
            executor=PatchExecutor(manager=manager),
            test_runner_factory=lambda _root: ReportQueue(reports),
            gate_runner=gate or (lambda _root: {"ok": True}),
            promote_runner=self.promote,
            max_rounds=max_rounds,
            guard=self.guard,
            lock=self.lock,
            journal=self.journal,
        )


# ── 并发控制 ──────────────────────────────────────────────────────


def test_second_repair_is_rejected_while_lock_is_held(slots, tmp_path):
    lock = RepairLock(tmp_path / "state" / "locks" / "repair.lock")
    holder = lock.acquire(run_id="other-run")
    assert holder is not None
    harness = Harness(
        slots, tmp_path,
        plans=[_plan()], reports=[_passed()],
        lock=RepairLock(tmp_path / "state" / "locks" / "repair.lock"),
    )

    record = harness.loop.run("fix existing.py")

    assert record["status"] == "repair_busy"
    assert record["ok"] is False
    assert record["failure_reason"] == "concurrent_repair_in_progress"
    assert record["lock"]["acquired"] is False
    assert record["lock"]["holder"]["run_id"] == "other-run"
    # 重复请求不得触碰候选代码，也不得调用规划器。
    assert harness.planner.calls == 0
    assert list(_candidate_app(slots).iterdir()) == []
    assert lock.release(holder) is True


def test_duplicate_request_succeeds_after_previous_run_finished(slots, tmp_path):
    harness = Harness(slots, tmp_path, plans=[_plan("v1")], reports=[_passed()])

    first = harness.loop.run("fix existing.py")
    second_harness = Harness(slots, tmp_path, plans=[_plan("v2")], reports=[_passed()])
    second = second_harness.loop.run("fix existing.py")

    assert first["status"] == "tests_passed"
    assert second["status"] == "tests_passed"
    assert second["lock"]["acquired"] is True


def test_stale_lock_from_dead_process_is_taken_over(slots, tmp_path):
    path = tmp_path / "state" / "locks" / "repair.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"run_id": "dead-run", "token": "dead", "pid": 999999, "ts": 0}),
        encoding="utf-8",
    )
    harness = Harness(slots, tmp_path, plans=[_plan()], reports=[_passed()])

    record = harness.loop.run("fix existing.py")

    assert record["status"] == "tests_passed"
    assert record["lock"]["acquired"] is True
    assert record["lock"]["released"] is True


def test_lock_release_does_not_delete_another_owners_lock(tmp_path):
    path = tmp_path / "locks" / "repair.lock"
    lock = RepairLock(path)
    handle = lock.acquire(run_id="r1")
    assert handle is not None

    # 模拟锁被另一个运行实例接管（内容被原子替换为新 owner）。
    path.write_text(
        json.dumps({"run_id": "r2", "token": "other", "pid": os.getpid(), "ts": 0}),
        encoding="utf-8",
    )

    assert lock.release(handle) is False
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "r2"


def test_lock_is_released_after_run(slots, tmp_path):
    harness = Harness(slots, tmp_path, plans=[_plan()], reports=[_passed()])

    record = harness.loop.run("fix existing.py")

    assert record["lock"]["released"] is True
    assert not (tmp_path / "state" / "locks" / "repair.lock").exists()


# ── 候选版本隔离 ──────────────────────────────────────────────────


def test_apply_failure_restores_candidate_to_baseline(slots, tmp_path):
    plan = _plan(target="missing.py")
    harness = Harness(slots, tmp_path, plans=[plan], reports=[_passed()])

    record = harness.loop.run("fix missing.py")

    assert record["status"] == "apply_failed"
    assert record["ok"] is False
    assert (_candidate_app(slots) / "existing.py").read_text(encoding="utf-8") == BASELINE_TEXT
    assert record["candidate"]["recovery"]["result"]["ok"] is True
    # 回到基线后不再保留"不可晋升"标记。
    assert read_candidate_state(_slot_root(slots, "B")) == {}


def test_tests_failure_restores_candidate_and_clears_marker(slots, tmp_path):
    harness = Harness(
        slots, tmp_path,
        plans=[_plan("v1"), _plan("v2")],
        reports=[_failed(), _failed("still broken")],
        max_rounds=1,
    )

    record = harness.loop.run("fix existing.py")

    assert record["status"] == "tests_failed"
    assert record["ok"] is False
    assert (_candidate_app(slots) / "existing.py").read_text(encoding="utf-8") == BASELINE_TEXT
    assert record["candidate"]["recovery"]["result"]["ok"] is True
    assert read_candidate_state(_slot_root(slots, "B")) == {}


def test_gate_failure_restores_candidate(slots, tmp_path):
    harness = Harness(
        slots, tmp_path,
        plans=[_plan()], reports=[_passed()],
        gate=lambda _root: {"ok": False, "error": "contract_failed"},
    )

    record = harness.loop.run("fix existing.py")

    assert record["status"] == "gate_failed"
    assert record["ok"] is False
    assert (_candidate_app(slots) / "existing.py").read_text(encoding="utf-8") == BASELINE_TEXT
    assert harness.promote.calls == 0
    assert read_candidate_state(_slot_root(slots, "B")) == {}


def test_restore_failure_quarantines_candidate(slots, tmp_path):
    active_file = tmp_path / "slots" / "A" / "app" / "existing.py"

    def tamper_then_fail():
        # 运行期间改动活跃槽位：基线漂移，回滚无法确定正确内容。
        active_file.write_text("drifted\n", encoding="utf-8")
        return _failed()

    harness = Harness(
        slots, tmp_path,
        plans=[_plan()], reports=[tamper_then_fail], max_rounds=0,
    )

    record = harness.loop.run("fix existing.py")

    assert record["status"] == "tests_failed"
    assert record["candidate"]["recovery"]["result"]["ok"] is False
    assert record["candidate"]["recovery"]["quarantined"] is True
    state = read_candidate_state(_slot_root(slots, "B"))
    assert state["promotable"] is False
    assert state["reason"].startswith("restore_failed")


def test_candidate_changed_after_gate_blocks_promotion(slots, tmp_path):
    candidate_file = _candidate_app(slots) / "existing.py"

    def gate_then_tamper(root):
        # 模拟"验证之后代码又变了"：闸门期间候选代码被改写。
        Path(root, "existing.py").write_text("tampered\n", encoding="utf-8")
        return {"ok": True}

    harness = Harness(
        slots, tmp_path,
        plans=[_plan()], reports=[_passed()],
        gate=gate_then_tamper,
    )

    record = harness.loop.run("fix existing.py", promote=True)

    assert record["status"] == "candidate_changed"
    assert record["ok"] is False
    assert harness.promote.calls == 0
    assert record["candidate"]["verifications"][-1]["reason"] == (
        "candidate_fingerprint_mismatch"
    )
    # 失败代码被回滚，不留在候选槽位。
    assert candidate_file.read_text(encoding="utf-8") == BASELINE_TEXT


def test_success_binds_baseline_patch_and_candidate_fingerprint(slots, tmp_path):
    harness = Harness(slots, tmp_path, plans=[_plan("v1")], reports=[_passed()])

    record = harness.loop.run("fix existing.py")

    candidate_app = _candidate_app(slots)
    assert record["status"] == "tests_passed"
    assert record["candidate"]["baseline"]["slot"] == "A"
    assert record["candidate"]["baseline"]["version"] == "0.1.0"
    assert record["candidate"]["candidate_fingerprint"] == tree_fingerprint(candidate_app)
    assert record["candidate"]["patch_fingerprint"] == record["final_patch_fingerprint"]
    assert record["candidate"]["promotable"] is True
    assert record["run_id"]

    state = read_candidate_state(_slot_root(slots, "B"))
    assert state["promotable"] is True
    assert state["fingerprint"] == record["candidate"]["candidate_fingerprint"]
    assert state["run_id"] == record["run_id"]


def test_journal_records_phases_and_terminal_status(slots, tmp_path):
    harness = Harness(slots, tmp_path, plans=[_plan()], reports=[_passed()])

    record = harness.loop.run("fix existing.py")

    entry = harness.journal.read(record["run_id"])
    assert entry["status"] == "tests_passed"
    assert entry["ok"] is True
    for phase in ("locked", "applying", "applied", "finished"):
        assert phase in entry["phases"]
    assert entry["candidate_fingerprint"] == record["candidate"]["candidate_fingerprint"]


# ── 中断恢复 ──────────────────────────────────────────────────────


def _write_interrupted_journal(
    harness, slots, *, run_id, pid, baseline, phase="applying", content="broken\n",
):
    """直接落一份"崩溃进程"留下的运行日志（pid 可控）。"""
    candidate_app = _candidate_app(slots)
    candidate_app.mkdir(parents=True, exist_ok=True)
    (candidate_app / "existing.py").write_text(content, encoding="utf-8")
    harness.guard.mark_not_promotable("B", reason="repair_in_progress", run_id=run_id)
    payload = {
        "run_id": run_id,
        "pid": pid,
        "status": "started",
        "ok": False,
        "phase": phase,
        "phases": ["locked", "applying", phase],
        "token": "old-token",
        "started_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "finished_at": None,
        "baseline": baseline.to_dict() if baseline else None,
        "candidate_root": str(candidate_app),
        "candidate_fingerprint": tree_fingerprint(candidate_app),
        "patch_fingerprint": "old-patch",
        "failure_reason": None,
    }
    path = harness.journal.path_for(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_interrupted_run_is_restored_before_next_run(slots, tmp_path):
    harness = Harness(slots, tmp_path, plans=[{"operations": []}], reports=[_passed()])
    baseline = harness.guard.capture_baseline()
    _write_interrupted_journal(
        harness, slots, run_id="interrupted-run", pid=999999, baseline=baseline,
    )

    record = harness.loop.run("next repair")

    assert record["status"] == "blocked"
    assert record["recovery"][0]["action"] == "restored"
    assert (_candidate_app(slots) / "existing.py").read_text(encoding="utf-8") == BASELINE_TEXT
    entry = harness.journal.read("interrupted-run")
    assert entry["status"] == "interrupted_recovered"
    assert entry["ok"] is False


def test_interrupted_run_without_baseline_is_quarantined(slots, tmp_path):
    harness = Harness(slots, tmp_path, plans=[{"operations": []}], reports=[_passed()])
    _write_interrupted_journal(
        harness, slots, run_id="no-baseline", pid=999999, baseline=None,
    )

    record = harness.loop.run("next repair")

    assert record["recovery"][0]["action"] == "quarantined"
    assert record["recovery"][0]["quarantined"] is True
    state = read_candidate_state(_slot_root(slots, "B"))
    assert state["promotable"] is False
    assert state["reason"] == "interrupted_without_baseline"
    assert harness.journal.read("no-baseline")["status"] == "interrupted"


def test_interrupted_run_with_live_owner_is_skipped(slots, tmp_path, monkeypatch):
    monkeypatch.setattr(guard_module, "pid_alive", lambda pid: True)
    harness = Harness(slots, tmp_path, plans=[{"operations": []}], reports=[_passed()])
    baseline = harness.guard.capture_baseline()
    _write_interrupted_journal(
        harness, slots, run_id="live-run", pid=424242, baseline=baseline,
    )

    record = harness.loop.run("next repair")

    assert record["recovery"][0]["action"] == "skipped_live_owner"
    # 活着的运行实例的候选代码不得被动过。
    assert (_candidate_app(slots) / "existing.py").read_text(encoding="utf-8") == "broken\n"
    assert harness.journal.read("live-run")["status"] == "started"


def test_recovery_is_idempotent_for_already_restored_candidate(slots, tmp_path):
    harness = Harness(slots, tmp_path, plans=[{"operations": []}], reports=[_passed()])
    baseline = harness.guard.capture_baseline()
    _write_interrupted_journal(
        harness, slots, run_id="half-run", pid=999999, baseline=baseline,
    )
    # 手工把候选槽位恢复为基线（模拟恢复已发生但日志未收尾）。
    sync_app_tree(_slot_root(slots, "A") / "app", _candidate_app(slots))

    record = harness.loop.run("next repair")

    assert record["recovery"][0]["action"] == "noop_already_baseline"
    assert harness.journal.read("half-run")["status"] == "interrupted_recovered"


def test_interrupted_promotion_is_left_to_supervisor(slots, tmp_path):
    harness = Harness(slots, tmp_path, plans=[{"operations": []}], reports=[_passed()])
    baseline = harness.guard.capture_baseline()
    _write_interrupted_journal(
        harness, slots,
        run_id="promoting-run", pid=999999, baseline=baseline,
        phase="promoting", content="verified\n",
    )

    record = harness.loop.run("next repair")

    assert record["recovery"][0]["action"] == "promotion_pending"
    # 已通过闸门的候选代码必须原样保留：Supervisor 可能正在执行这次晋升。
    assert (_candidate_app(slots) / "existing.py").read_text(encoding="utf-8") == "verified\n"
    entry = harness.journal.read("promoting-run")
    assert entry["status"] == "interrupted_promotion_pending"
    assert entry["ok"] is False


def test_recovery_skips_run_that_was_superseded_by_promotion(slots, tmp_path):
    harness = Harness(slots, tmp_path, plans=[{"operations": []}], reports=[_passed()])
    baseline = harness.guard.capture_baseline()
    _write_interrupted_journal(
        harness, slots, run_id="promoted-run", pid=999999, baseline=baseline,
    )
    slots.set_active_slot("B")  # 基线槽位 A 已不再活跃

    results = recover_interrupted(harness.journal, harness.guard)

    assert results[0]["action"] == "superseded"
    assert harness.journal.read("promoted-run")["status"] == "interrupted_superseded"
    assert harness.journal.read("promoted-run")["ok"] is False


# ── 晋升门禁核对 ──────────────────────────────────────────────────


def _release(tmp_path, manager):
    return ReleaseManager(
        manager=manager,
        probe=HealthyProbe(),  # type: ignore[arg-type]
        state_file=tmp_path / "runtime" / "release_state.json",
        gate=PassGate(),  # type: ignore[arg-type]
    )


def test_release_manager_blocks_slot_marked_not_promotable(tmp_path, slots):
    write_candidate_state(
        _slot_root(slots, "B"),
        {"promotable": False, "reason": "repair_in_progress", "fingerprint": ""},
    )

    result = _release(tmp_path, slots).promote()

    assert result["ok"] is False
    assert "candidate_not_promotable" in result["error"]
    assert slots.get_active_slot() == "A"


def test_release_manager_blocks_fingerprint_mismatch(tmp_path, slots):
    write_candidate_state(
        _slot_root(slots, "B"),
        {"promotable": True, "reason": "", "fingerprint": "deadbeef"},
    )

    result = _release(tmp_path, slots).promote()

    assert result["ok"] is False
    assert "candidate_fingerprint_mismatch" in result["error"]
    assert slots.get_active_slot() == "A"


def test_release_manager_promotes_when_fingerprint_matches(tmp_path, slots):
    write_candidate_state(
        _slot_root(slots, "B"),
        {
            "promotable": True,
            "reason": "",
            "fingerprint": tree_fingerprint(_slot_root(slots, "B") / "app"),
        },
    )

    result = _release(tmp_path, slots).promote()

    assert result["ok"] is True
    assert slots.get_active_slot() == "B"


def test_release_manager_without_candidate_state_keeps_previous_behavior(tmp_path, slots):
    result = _release(tmp_path, slots).promote()

    assert result["ok"] is True
    assert slots.get_active_slot() == "B"


# ── 手动 apply 入口的并发保护 ─────────────────────────────────────


def _apply_operations():
    return [{
        "operation": "modify_file",
        "target_file": "existing.py",
        "content": "manual\n",
        "reason": "manual",
        "not_allowed_yet": False,
    }]


def test_manual_apply_is_rejected_while_repair_lock_is_held(slots, tmp_path):
    lock = RepairLock(tmp_path / "state" / "locks" / "repair.lock")
    holder = lock.acquire(run_id="repair-run")
    assert holder is not None

    result = apply_candidate_patch(
        PatchExecutor(manager=slots), _apply_operations(), lock=lock,
    )

    assert result["ok"] is False
    assert result["error"] == "concurrent_repair_in_progress"
    assert result["holder"]["run_id"] == "repair-run"
    assert list(_candidate_app(slots).iterdir()) == []
    assert lock.release(holder) is True


def test_manual_apply_marks_promotable_when_tests_pass(slots, tmp_path):
    lock = RepairLock(tmp_path / "state" / "locks" / "repair.lock")

    result = apply_candidate_patch(
        PatchExecutor(manager=slots), _apply_operations(), lock=lock,
    )
    state = finalize_candidate_patch(
        result, tests_ok=True, guard=CandidateSlotGuard(manager=slots),
    )

    assert result["ok"] is True
    assert result["lock"]["released"] is True
    assert state["promotable"] is True
    marker = read_candidate_state(_slot_root(slots, "B"))
    assert marker["promotable"] is True
    assert marker["fingerprint"] == result["candidate"]["candidate_fingerprint"]
    assert not lock.path.exists()


def test_manual_apply_with_failing_tests_blocks_promotion(slots, tmp_path):
    lock = RepairLock(tmp_path / "state" / "locks" / "repair.lock")

    result = apply_candidate_patch(
        PatchExecutor(manager=slots), _apply_operations(), lock=lock,
    )
    state = finalize_candidate_patch(
        result, tests_ok=False, guard=CandidateSlotGuard(manager=slots),
    )

    assert state["promotable"] is False
    blocked = _release(tmp_path, slots).promote()
    assert blocked["ok"] is False
    assert "candidate_not_promotable" in blocked["error"]
    assert slots.get_active_slot() == "A"
