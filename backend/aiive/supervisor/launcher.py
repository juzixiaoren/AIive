"""
槽位启动与管理模块。

提供槽位状态查询和活跃槽位健康检查功能。
结合 SlotManager 和 HealthProbe，向上层提供统一的槽位管理接口。
"""
import logging

from aiive.supervisor.health_probe import HealthProbe
from aiive.supervisor.slot_manager import SlotManager

logger = logging.getLogger(__name__)


class Launcher:
    """槽位启动器，提供状态查询和健康检查。"""

    def __init__(self, manager: SlotManager | None = None):
        """
        初始化启动器。

        参数:
            manager: 槽位管理器实例，默认自动创建。
        """
        self._manager = manager or SlotManager()
        self._probe = HealthProbe()

    def get_status(self) -> dict:
        """
        获取所有槽位的状态概览。

        返回:
            包含 active_slot（当前活跃槽位名称）和 slots（所有槽位详情列表）的字典。
            每个槽位详情包括 name、active、manifest_version、manifest_checksum、
            healthy 和 health_checks 字段。
        """
        try:
            active = self._manager.get_active_slot()
            slots = self._manager.list_slots()
            slot_data = []
            for s in slots:
                # 对每个槽位执行健康检查
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
        except Exception:
            logger.exception("获取槽位状态失败")
            raise

    def health_check(self) -> dict:
        """
        对当前活跃槽位执行健康检查。

        返回:
            包含 healthy、active_slot、message 和 checks 的健康检查结果字典。
            如果活跃槽位未找到，返回 healthy=False。
        """
        try:
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
        except Exception:
            logger.exception("槽位健康检查失败")
            raise
