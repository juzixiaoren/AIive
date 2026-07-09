from aiive.supervisor.health_probe import HealthProbe
from aiive.supervisor.slot_manager import SlotManager


class PromoteRollback:
    def __init__(self, manager: SlotManager | None = None):
        self._manager = manager or SlotManager()
        self._probe = HealthProbe()

    def promote(self) -> dict:
        slots = self._manager.list_slots()
        active = self._manager.get_active_slot()
        inactive = "B" if active == "A" else "A"

        inactive_slot = next((s for s in slots if s.name == inactive), None)
        if not inactive_slot:
            return {"ok": False, "error": "Inactive slot not found"}

        # Health check inactive
        result = self._probe.check(inactive, str(inactive_slot.root))
        if not result.healthy:
            return {
                "ok": False,
                "error": "Health check failed for inactive slot",
                "health": result.message,
                "checks": [c for c in result.checks if not c.get("ok", True)],
            }

        # Swap active pointer
        self._manager.set_active_slot(inactive)
        return {
            "ok": True,
            "previous_active": active,
            "new_active": inactive,
        }

    def rollback(self, previous_active: str) -> dict:
        current = self._manager.get_active_slot()

        # Health check previous
        slots = self._manager.list_slots()
        prev_slot = next((s for s in slots if s.name == previous_active), None)
        if not prev_slot:
            return {"ok": False, "error": f"Previous slot {previous_active} not found"}

        result = self._probe.check(previous_active, str(prev_slot.root))
        if not result.healthy:
            return {
                "ok": False,
                "error": f"Previous slot {previous_active} is unhealthy",
                "health": result.message,
            }

        self._manager.set_active_slot(previous_active)
        return {
            "ok": True,
            "rolled_back_from": current,
            "rolled_back_to": previous_active,
        }
