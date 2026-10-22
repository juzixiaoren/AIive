"""ReleaseManager 测试：观察窗自动回滚 + 晋升安全门（fail-closed）集成。

安全门通过构造函数注入（FakeGate），避免单元测试真实跑契约/重放；
真实闸门行为见 tests/unit/backend/test_eval_gate.py。
"""
from dataclasses import dataclass, field

from aiive.eval.models import GateResult
from aiive.supervisor.health_probe import HealthResult
from aiive.supervisor.release_manager import ReleaseManager
from aiive.supervisor.slot_manager import SlotManager


@dataclass
class MutableProbe:
    unhealthy_slots: set[str] | None = None

    def check(self, slot: str, slot_root: str) -> HealthResult:
        healthy = slot not in (self.unhealthy_slots or set())
        return HealthResult(healthy=healthy, slot=slot, message="ok" if healthy else "failed")


@dataclass
class FakeGate:
    """记录调用的 SafetyGate 替身。"""

    ok: bool = True
    calls: list[str | None] = field(default_factory=list)

    def evaluate(self, candidate_app_dir: str | None = None, persist: bool = True) -> GateResult:
        self.calls.append(candidate_app_dir)
        return GateResult(ok=self.ok, error="" if self.ok else "simulated gate failure")


def _release(tmp_path, gate: FakeGate | None = None):
    """构造 (ReleaseManager, SlotManager, MutableProbe) 三件套。"""
    manager = SlotManager(base_dir=tmp_path / "slots")
    manager.init_slots()
    probe = MutableProbe()
    release = ReleaseManager(
        manager=manager,
        probe=probe,  # type: ignore[arg-type]
        state_file=tmp_path / "runtime" / "release_state.json",
        gate=gate,  # type: ignore[arg-type]
    )
    return release, manager, probe


def test_release_monitor_automatically_rolls_back_unhealthy_candidate(tmp_path) -> None:
    release, manager, probe = _release(tmp_path, gate=FakeGate(ok=True))

    promoted = release.promote()
    assert promoted["ok"] is True
    assert manager.get_active_slot() == "B"

    probe.unhealthy_slots = {"B"}
    monitored = release.monitor_once()

    assert monitored["action"] == "auto_rollback"
    assert monitored["ok"] is True
    assert manager.get_active_slot() == "A"
    assert release.read_state()["status"] == "rolled_back"


def test_release_promote_runs_gate_before_switching(tmp_path) -> None:
    """默认 promote 必须经过安全门；闸门通过后才切换活跃指针。"""
    gate = FakeGate(ok=True)
    release, manager, _probe = _release(tmp_path, gate=gate)

    promoted = release.promote()
    assert promoted["ok"] is True
    assert promoted["new_active"] == "B"
    assert manager.get_active_slot() == "B"
    # init_slots 会创建 <slot>/app 目录 → 闸门收到候选 app 路径（B 槽）
    assert gate.calls == [str(tmp_path / "slots" / "B" / "app")]


def test_release_promote_blocked_when_gate_fails(tmp_path) -> None:
    """闸门失败时 fail-closed：不切槽、返回闸门摘要。"""
    gate = FakeGate(ok=False)
    release, manager, _probe = _release(tmp_path, gate=gate)

    promoted = release.promote()
    assert promoted["ok"] is False
    assert "Safety gate failed" in promoted["error"]
    assert promoted["gate"]["ok"] is False
    assert manager.get_active_slot() == "A"


def test_release_promote_can_skip_gate_explicitly(tmp_path) -> None:
    """run_gate=False 显式跳过（仅限测试/受控调用），闸门不被调用。"""
    gate = FakeGate(ok=False)
    release, manager, _probe = _release(tmp_path, gate=gate)

    promoted = release.promote(run_gate=False)
    assert promoted["ok"] is True
    assert manager.get_active_slot() == "B"
    assert gate.calls == []
