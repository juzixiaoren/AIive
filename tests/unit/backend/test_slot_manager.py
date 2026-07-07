import json
from pathlib import Path

from aiive.supervisor.slot_manager import (
    SlotManager,
    _compute_manifest_checksum,
    create_version_manifest,
)


class TestSlotManager:
    def test_get_active_defaults_to_a(self, tmp_path):
        mgr = SlotManager(base_dir=tmp_path / "slots")
        assert mgr.get_active_slot() == "A"

    def test_set_and_get_active(self, tmp_path):
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.set_active_slot("B")
        assert mgr.get_active_slot() == "B"

    def test_init_slots_creates_directories_and_manifest(self, tmp_path):
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
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()
        assert mgr.get_active_slot() == "A"

    def test_list_slots_returns_a_and_b(self, tmp_path):
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()
        slots = mgr.list_slots()
        names = {s.name for s in slots}
        assert names == {"A", "B"}

    def test_list_slots_marks_active(self, tmp_path):
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()
        mgr.set_active_slot("B")
        slots = mgr.list_slots()
        a = next(s for s in slots if s.name == "A")
        b = next(s for s in slots if s.name == "B")
        assert a.active is False
        assert b.active is True


class TestVersionManifest:
    def test_excludes_data_directories(self):
        manifest = create_version_manifest("A")
        excludes = manifest["excludes"]
        for item in ["data/", "postgres/", "qdrant/", "object_store/", "logs/"]:
            assert item in excludes

    def test_manifest_not_include_env(self):
        manifest = create_version_manifest("A")
        assert ".env" in manifest["excludes"]

    def test_manifest_checksum_stable(self):
        m1 = create_version_manifest("A")
        m2 = create_version_manifest("A")
        assert _compute_manifest_checksum(m1) == _compute_manifest_checksum(m2)

    def test_manifest_checksum_different_for_different_slots(self):
        m1 = create_version_manifest("A")
        m2 = create_version_manifest("B")
        assert _compute_manifest_checksum(m1) != _compute_manifest_checksum(m2)
