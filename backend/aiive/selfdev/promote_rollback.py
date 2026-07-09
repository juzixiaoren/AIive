"""
槽位升级与回滚模块。

提供 A/B 槽位的 promote（升级）和 rollback（回滚）操作。
升级前会对非活跃槽位进行健康检查，回滚前会对目标槽位进行健康检查，
确保切换操作的安全性。
"""
import logging

from aiive.supervisor.health_probe import HealthProbe
from aiive.supervisor.slot_manager import SlotManager

logger = logging.getLogger(__name__)


class PromoteRollback:
    """槽位升级与回滚管理器。"""

    def __init__(self, manager: SlotManager | None = None):
        """
        初始化升级回滚管理器。

        参数:
            manager: 槽位管理器实例，默认自动创建。
        """
        self._manager = manager or SlotManager()
        self._probe = HealthProbe()

    def promote(self) -> dict:
        """
        将非活跃槽位升级为活跃槽位。

        流程：先对非活跃槽位执行健康检查，通过后切换活跃指针。

        返回:
            包含 ok、previous_active、new_active 的结果字典。
            健康检查失败时返回 ok=False 及失败原因。
        """
        slots = self._manager.list_slots()
        active = self._manager.get_active_slot()
        inactive = "B" if active == "A" else "A"

        inactive_slot = next((s for s in slots if s.name == inactive), None)
        if not inactive_slot:
            return {"ok": False, "error": "Inactive slot not found"}

        # 对非活跃槽位执行健康检查
        result = self._probe.check(inactive, str(inactive_slot.root))
        if not result.healthy:
            return {
                "ok": False,
                "error": "Health check failed for inactive slot",
                "health": result.message,
                "checks": [c for c in result.checks if not c.get("ok", True)],
            }

        # 切换活跃槽位指针
        self._manager.set_active_slot(inactive)
        return {
            "ok": True,
            "previous_active": active,
            "new_active": inactive,
        }

    def rollback(self, previous_active: str) -> dict:
        """
        回滚到指定的之前活跃槽位。

        流程：对目标槽位执行健康检查，通过后切换活跃指针。

        参数:
            previous_active: 要回滚到的槽位名称（"A" 或 "B"）。

        返回:
            包含 ok、rolled_back_from、rolled_back_to 的结果字典。
        """
        current = self._manager.get_active_slot()

        # 对目标回滚槽位执行健康检查
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
