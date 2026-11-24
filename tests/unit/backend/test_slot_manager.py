"""测试 SlotManager（槽位管理器）和版本清单（Version Manifest）功能。

覆盖槽位初始化、切换活跃槽位、清单校验和等操作。
"""

import json
from pathlib import Path

from aiive.supervisor.slot_manager import (
    ACTIVE_SLOT_FILE,
    SLOTS_ROOT,
    SlotManager,
    _compute_manifest_checksum,
    create_version_manifest,
)


class TestDefaultPaths:
    """回归：SLOTS_ROOT/ACTIVE_SLOT_FILE 必须位于仓库根下（曾多算一层 parent）。"""

    def test_slots_root_inside_repo(self):
        repo_root = SLOTS_ROOT.parent
        assert (repo_root / "backend" / "aiive").is_dir()
        assert SLOTS_ROOT.name == "slots"

    def test_active_slot_file_inside_repo(self):
        repo_root = ACTIVE_SLOT_FILE.parent.parent
        assert (repo_root / "backend" / "aiive").is_dir()
        assert ACTIVE_SLOT_FILE.parent.name == "runtime"


class TestSlotManager:
    """测试 SlotManager 的槽位管理功能。"""

    def test_get_active_defaults_to_a(self, tmp_path):
        """默认活跃槽位为 A。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        assert mgr.get_active_slot() == "A"

    def test_set_and_get_active(self, tmp_path):
        """设置和获取活跃槽位应一致。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.set_active_slot("B")
        assert mgr.get_active_slot() == "B"

    def test_init_slots_creates_directories_and_manifest(self, tmp_path):
        """初始化槽位应创建目录和版本清单文件。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()

        slot_a = tmp_path / "slots" / "A"
        assert slot_a.exists()
        manifest = slot_a / "version_manifest.json"
        assert manifest.exists()

        data = json.loads(manifest.read_text())
        assert data["slot"] == "A"
        assert "data/" in data["excludes"]
        assert ".env" in data["excludes"]

    def test_init_sets_active_to_a(self, tmp_path):
        """初始化后活跃槽位应为 A。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()
        assert mgr.get_active_slot() == "A"

    def test_list_slots_returns_a_and_b(self, tmp_path):
        """list_slots 应返回 A 和 B 两个槽位。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()
        slots = mgr.list_slots()
        names = {s.name for s in slots}
        assert names == {"A", "B"}

    def test_list_slots_marks_active(self, tmp_path):
        """list_slots 应正确标记活跃槽位。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()
        mgr.set_active_slot("B")
        slots = mgr.list_slots()
        a = next(s for s in slots if s.name == "A")
        b = next(s for s in slots if s.name == "B")
        assert a.active is False
        assert b.active is True


class TestVersionManifest:
    """测试版本清单的生成和校验。"""

    def test_excludes_data_directories(self):
        """版本清单应排除数据目录。"""
        manifest = create_version_manifest("A")
        excludes = manifest["excludes"]
        for item in ["data/", "postgres/", "qdrant/", "object_store/", "logs/"]:
            assert item in excludes

    def test_manifest_not_include_env(self):
        """版本清单应排除 .env 文件。"""
        manifest = create_version_manifest("A")
        assert ".env" in manifest["excludes"]

    def test_manifest_checksum_stable(self):
        """相同槽位的清单校验和应一致。"""
        m1 = create_version_manifest("A")
        m2 = create_version_manifest("A")
        assert _compute_manifest_checksum(m1) == _compute_manifest_checksum(m2)

    def test_manifest_checksum_different_for_different_slots(self):
        """不同槽位的清单校验和应不同。"""
        m1 = create_version_manifest("A")
        m2 = create_version_manifest("B")
        assert _compute_manifest_checksum(m1) != _compute_manifest_checksum(m2)

def test_concurrent_bootstrap_preserves_existing_active_slot(tmp_path):
    import os
    import subprocess
    import sys

    source = tmp_path / "source"
    (source / "backend" / "aiive").mkdir(parents=True)
    (source / "backend" / "aiive" / "main.py").write_text("VERSION = 1\n", encoding="utf-8")
    (source / ".env").write_text("FIXTURE_ONLY=1\n", encoding="utf-8")
    (source / ".env.example").write_text("FIXTURE_ONLY=\n", encoding="utf-8")
    manager = SlotManager(tmp_path / "slots")
    env = {**os.environ, "AIIVE_SOURCE_ROOT": str(source), "AIIVE_SLOTS_ROOT": str(manager.base_dir),
           "AIIVE_RUNTIME_DIR": str(manager.runtime_dir), "AIIVE_DATA_DIR": str(tmp_path / "data"),
           "PYTHONPATH": str(Path.cwd() / "backend")}
    command = [sys.executable, "-c",
               "from aiive.project_paths import source_root; "
               "from aiive.supervisor.slot_manager import SlotManager; "
               "SlotManager().ensure_initialized(source_root())"]
    workers = [subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
               for _ in range(2)]
    for worker in workers:
        _, errors = worker.communicate(timeout=30)
        assert worker.returncode == 0, errors.decode(errors="replace")
    assert manager.get_active_slot() == "A"
    assert (manager.active_app_root() / "backend/aiive/main.py").is_file()
    assert (manager.active_app_root() / ".env.example").is_file()
    assert not (manager.active_app_root() / ".env").exists()
    assert not list((manager.base_dir / "B/app").iterdir())
    before = manager.list_slots()
    manager.set_active_slot("B")
    manager.ensure_initialized(source)
    assert manager.get_active_slot() == "B"
    assert [slot.manifest for slot in manager.list_slots()] == [slot.manifest for slot in before]
