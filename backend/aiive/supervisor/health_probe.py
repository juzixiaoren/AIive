"""
槽位健康探测模块。

对 A/B 槽位执行多项健康检查，包括：
- manifest 文件是否存在
- app 目录是否存在
- 后端模块是否能正常导入
用于升级/回滚前的安全验证。
"""

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class HealthResult:
    """健康检查结果数据类。"""
    healthy: bool  # 是否健康
    slot: str  # 槽位名称
    message: str = ""  # 健康状态描述
    checks: list[dict] = None  # 各项检查详情

    def __post_init__(self):
        if self.checks is None:
            self.checks = []


class HealthProbe:
    """槽位健康探测器，执行多项检查并汇总结果。"""

    def check(self, slot: str, slot_root: str) -> HealthResult:
        """
        对指定槽位执行健康检查。

        参数:
            slot: 槽位名称（"A" 或 "B"）。
            slot_root: 槽位根目录路径。

        返回:
            HealthResult 包含 healthy、slot、message 和 checks 详情。
        """
        checks: list[dict] = []
        healthy = True

        # 检查 1：manifest 文件是否存在
        manifest_path = os.path.join(slot_root, "version_manifest.json")
        manifest_ok = os.path.exists(manifest_path)
        checks.append({"name": "manifest_exists", "ok": manifest_ok})
        if not manifest_ok:
            healthy = False

        # 检查 2：app 目录是否存在
        app_dir = os.path.join(slot_root, "app")
        app_ok = os.path.isdir(app_dir)
        checks.append({"name": "app_dir_exists", "ok": app_ok})

        # 检查 3：后端模块能否正常导入
        try:
            import importlib
            importlib.import_module("aiive.main")
            backend_ok = True
        except Exception:
            logger.warning("后端模块导入失败", exc_info=True)
            backend_ok = False
        checks.append({"name": "backend_import", "ok": backend_ok})

        return HealthResult(
            healthy=healthy,
            slot=slot,
            message="Healthy" if healthy else "Unhealthy",
            checks=checks,
        )
