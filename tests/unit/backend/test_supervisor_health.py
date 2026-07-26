"""测试 Supervisor 的健康探针（HealthProbe）和启动器（Launcher）模块。

验证槽位健康检查和启动状态查询功能。
"""

import json
from pathlib import Path

from aiive.supervisor.health_probe import HealthProbe
from aiive.supervisor.launcher import Launcher
from aiive.supervisor.slot_manager import SlotManager, create_version_manifest


class TestHealthProbe:
    """测试 HealthProbe 的槽位健康检查功能。"""

    def test_healthy_with_manifest(self, tmp_path):
        """含有完整清单和 app 目录的槽位应为健康状态。"""
        slot_dir = tmp_path / "A"
        slot_dir.mkdir()
        (slot_dir / "app").mkdir()
        (slot_dir / "version_manifest.json").write_text(
            json.dumps(create_version_manifest("A"))
        )

        probe = HealthProbe()
        result = probe.check("A", str(slot_dir))
        # manifest_exists 和 app_dir_exists 应为 ok
        checks_by_name = {c["name"]: c["ok"] for c in result.checks}
        assert checks_by_name["manifest_exists"] is True
        assert checks_by_name["app_dir_exists"] is True

    def test_missing_manifest_unhealthy(self, tmp_path):
        """缺少清单的槽位应为不健康状态。"""
        slot_dir = tmp_path / "B"
        slot_dir.mkdir()

        probe = HealthProbe()
        result = probe.check("B", str(slot_dir))
        checks_by_name = {c["name"]: c["ok"] for c in result.checks}
        assert checks_by_name["manifest_exists"] is False

    def test_missing_app_dir_unhealthy(self, tmp_path):
        """缺少 app 目录的槽位应为不健康（该检查必须影响结论）。"""
        slot_dir = tmp_path / "A"
        slot_dir.mkdir()
        (slot_dir / "version_manifest.json").write_text(
            json.dumps(create_version_manifest("A"))
        )

        probe = HealthProbe()
        result = probe.check("A", str(slot_dir))
        checks_by_name = {c["name"]: c["ok"] for c in result.checks}
        assert checks_by_name["app_dir_exists"] is False
        assert result.healthy is False

    def test_placeholder_slot_skips_backend_import(self, tmp_path):
        """无后端代码副本的占位槽位应跳过导入检查并保持健康。"""
        slot_dir = tmp_path / "A"
        slot_dir.mkdir()
        (slot_dir / "app").mkdir()
        (slot_dir / "version_manifest.json").write_text(
            json.dumps(create_version_manifest("A"))
        )

        probe = HealthProbe()
        result = probe.check("A", str(slot_dir))
        backend_check = next(c for c in result.checks if c["name"] == "backend_import")
        assert backend_check.get("skipped") is True
        assert result.healthy is True

    def test_broken_backend_payload_unhealthy(self, tmp_path):
        """槽位内存在 backend/aiive 但无法导入时，应判定为不健康。"""
        slot_dir = tmp_path / "A"
        (slot_dir / "app" / "backend" / "aiive").mkdir(parents=True)
        # 制造一个导入即失败的槽位副本
        (slot_dir / "app" / "backend" / "aiive" / "__init__.py").write_text("")
        (slot_dir / "app" / "backend" / "aiive" / "main.py").write_text(
            "raise RuntimeError('broken slot payload')"
        )
        (slot_dir / "version_manifest.json").write_text(
            json.dumps(create_version_manifest("A"))
        )

        probe = HealthProbe()
        result = probe.check("A", str(slot_dir))
        backend_check = next(c for c in result.checks if c["name"] == "backend_import")
        assert backend_check["ok"] is False
        assert result.healthy is False

    def test_returns_health_result(self, tmp_path):
        """健康检查应返回包含 healthy 属性和 slot 名称的结果。"""
        slot_dir = tmp_path / "A"
        slot_dir.mkdir()
        (slot_dir / "app").mkdir()
        (slot_dir / "version_manifest.json").write_text(
            json.dumps(create_version_manifest("A"))
        )

        probe = HealthProbe()
        result = probe.check("A", str(slot_dir))
        assert hasattr(result, "healthy")
        assert result.slot == "A"


class TestLauncher:
    """测试 Launcher 的状态查询和健康检查。"""

    def test_get_status(self, tmp_path):
        """get_status 应返回活跃槽位和槽位列表。"""
        slots_dir = tmp_path / "slots"
        mgr = SlotManager(base_dir=slots_dir)
        mgr.init_slots()

        launcher = Launcher(manager=mgr)
        status = launcher.get_status()
        assert status["active_slot"] == "A"
        assert len(status["slots"]) == 2

    def test_health_check(self, tmp_path):
        """health_check 应返回活跃槽位和健康状态。"""
        slots_dir = tmp_path / "slots"
        mgr = SlotManager(base_dir=slots_dir)
        mgr.init_slots()

        launcher = Launcher(manager=mgr)
        result = launcher.health_check()
        assert result["active_slot"] == "A"
        assert "healthy" in result
