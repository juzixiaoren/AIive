from aiive.selfdev.promote_rollback import PromoteRollback
from aiive.supervisor.slot_manager import SlotManager


class TestPromoteRollback:
    def test_promote_switches_active_slot(self, tmp_path):
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()
        pr = PromoteRollback(manager=mgr)

        result = pr.promote()
        assert result["ok"] is True
        assert result["previous_active"] == "A"
        assert result["new_active"] == "B"
        assert mgr.get_active_slot() == "B"

    def test_rollback_restores_active(self, tmp_path):
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()
        pr = PromoteRollback(manager=mgr)

        # Promote A→B first
        pr.promote()
        assert mgr.get_active_slot() == "B"

        # Then rollback B→A
        result = pr.rollback("A")
        assert result["ok"] is True
        assert mgr.get_active_slot() == "A"

    def test_promote_twice(self, tmp_path):
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()
        pr = PromoteRollback(manager=mgr)

        r1 = pr.promote()  # A → B
        assert r1["new_active"] == "B"

        r2 = pr.promote()  # B → A
        assert r2["new_active"] == "A"

    def test_rollback_invalid_slot(self, tmp_path):
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()
        pr = PromoteRollback(manager=mgr)

        result = pr.rollback("C")
        assert result["ok"] is False
        assert "not found" in result["error"].lower()
