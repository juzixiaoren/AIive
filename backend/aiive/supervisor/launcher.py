from aiive.supervisor.health_probe import HealthProbe
from aiive.supervisor.slot_manager import SlotManager


class Launcher:
    def __init__(self, manager: SlotManager | None = None):
        self._manager = manager or SlotManager()
        self._probe = HealthProbe()

    def get_status(self) -> dict:
        active = self._manager.get_active_slot()
        slots = self._manager.list_slots()
        slot_data = []
        for s in slots:
            result = self._probe.check(s.name, str(s.root))
            slot_data.append({
                "name": s.name,
                "active": s.active,
                "manifest_version": s.manifest.get("version", ""),
                "manifest_checksum": s.manifest_checksum,
                "healthy": result.healthy,
                "health_checks": [c["name"] for c in result.checks if not c.get("ok", True)],
            })

        return {
            "active_slot": active,
            "slots": slot_data,
        }

    def health_check(self) -> dict:
        active = self._manager.get_active_slot()
        slots = self._manager.list_slots()
        active_slot = next((s for s in slots if s.name == active), None)
        if not active_slot:
            return {"healthy": False, "active_slot": active, "message": "Active slot not found"}

        result = self._probe.check(active, str(active_slot.root))
        return {
            "healthy": result.healthy,
            "active_slot": active,
            "message": result.message,
            "checks": result.checks,
        }
