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
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class HealthResult:
    """健康检查结果数据类。"""
    healthy: bool  # 是否健康
    slot: str  # 槽位名称
    message: str = ""  # 健康状态描述
    checks: list[dict[str, Any]] = field(default_factory=list)  # 各项检查详情


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
        checks: list[dict[str, Any]] = []

        # 检查 1：manifest 文件是否存在
        manifest_path = os.path.join(slot_root, "version_manifest.json")
        manifest_ok = os.path.exists(manifest_path)
        checks.append({"name": "manifest_exists", "ok": manifest_ok})

        # 检查 2：app 目录是否存在
        app_dir = os.path.join(slot_root, "app")
        app_ok = os.path.isdir(app_dir)
        checks.append({"name": "app_dir_exists", "ok": app_ok})

        # 检查 3：槽位内后端模块能否正常导入。
        # 关键：用 subprocess 在槽位目录内导入（cwd/sys.path 指向槽位副本），
        # 而不是导入当前进程已加载的模块。占位槽位（app 内尚无 backend 代码
        # 副本）跳过该检查并注明，避免 bootstrap 阶段被永久封死。
        slot_backend = os.path.join(app_dir, "backend")
        if os.path.isdir(os.path.join(slot_backend, "aiive")):
            backend_ok = self._check_slot_backend_import(slot_backend, checks)
        else:
            checks.append({
                "name": "backend_import",
                "ok": True,
                "skipped": True,
                "reason": "slot has no backend payload (placeholder slot)",
            })
            backend_ok = True

        # 结论：所有检查全部通过才算健康
        healthy = manifest_ok and app_ok and backend_ok

        return HealthResult(
            healthy=healthy,
            slot=slot,
            message="Healthy" if healthy else "Unhealthy",
            checks=checks,
        )

    def _check_slot_backend_import(
        self, slot_backend: str, checks: list[dict[str, Any]]
    ) -> bool:
        """在槽位自己的代码副本上执行 `python -c "import aiive.main"`。

        cwd 与 PYTHONPATH 均指向槽位的 backend 目录，Windows 下使用
        sys.executable 而非硬编码解释器名。
        """
        env = dict(os.environ)
        env["PYTHONPATH"] = slot_backend
        try:
            proc = subprocess.run(
                [sys.executable, "-c", "import aiive.main"],
                cwd=slot_backend,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            backend_ok = proc.returncode == 0
            detail = (proc.stderr or "")[-500:] if not backend_ok else ""
        except Exception as e:
            logger.warning("槽位后端导入检查执行失败", exc_info=True)
            backend_ok = False
            detail = str(e)
        entry: dict[str, Any] = {"name": "backend_import", "ok": backend_ok}
        if detail:
            entry["detail"] = detail
        checks.append(entry)
        if not backend_ok:
            logger.warning("槽位后端模块导入失败: %s", detail)
        return backend_ok
