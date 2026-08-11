from dataclasses import dataclass

from aiive.supervisor.health_probe import HealthResult
from aiive.supervisor.release_manager import ReleaseManager
from aiive.supervisor.slot_manager import SlotManager


@dataclass
class MutableProbe:
    unhealthy_slots: set[str] | None = None

    def check(self, slot: str, slot_root: str) -> HealthResult:
        healthy = slot not in (self.unhealthy_slots or set())
        return HealthResult(healthy=healthy, slot=slot, message="ok" if healthy else "failed")


def test_release_monitor_automatically_rolls_back_unhealthy_candidate(tmp_path) -> None:
    manager = SlotManager(base_dir=tmp_path / "slots")
    manager.init_slots()
    probe = MutableProbe()
    release = ReleaseManager(
        manager=manager,
        probe=probe,  # type: ignore[arg-type]
        state_file=tmp_path / "runtime" / "release_state.json",
    )

    promoted = release.promote()
    assert promoted["ok"] is True
    assert manager.get_active_slot() == "B"

    probe.unhealthy_slots = {"B"}
    monitored = release.monitor_once()

    assert monitored["action"] == "auto_rollback"
    assert monitored["ok"] is True
    assert manager.get_active_slot() == "A"
    assert release.read_state()["status"] == "rolled_back"
