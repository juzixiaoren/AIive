"""测试 PromoteRollback（提升与回滚）模块。

覆盖自进化系统中槽位提升（A->B 切换活跃槽位）和回滚（恢复到之前版本）的功能。
"""

from aiive.selfdev.promote_rollback import PromoteRollback
from aiive.supervisor.slot_manager import SlotManager


class TestPromoteRollback:
    """测试 PromoteRollback 的槽位切换和回滚功能。"""

    def test_promote_switches_active_slot(self, tmp_path):
        """提升操作应切换活跃槽位从 A 到 B。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()
        pr = PromoteRollback(manager=mgr)

        result = pr.promote()
        assert result["ok"] is True
        assert result["previous_active"] == "A"
        assert result["new_active"] == "B"
        assert mgr.get_active_slot() == "B"

    def test_rollback_restores_active(self, tmp_path):
        """回滚操作应恢复活跃槽位。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()
        pr = PromoteRollback(manager=mgr)

        # 先提升 A -> B
        pr.promote()
        assert mgr.get_active_slot() == "B"

        # 再回滚 B -> A
        result = pr.rollback("A")
        assert result["ok"] is True
        assert mgr.get_active_slot() == "A"

    def test_promote_twice(self, tmp_path):
        """连续提升两次：A -> B -> A。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()
        pr = PromoteRollback(manager=mgr)

        r1 = pr.promote()  # A -> B
        assert r1["new_active"] == "B"

        r2 = pr.promote()  # B -> A
        assert r2["new_active"] == "A"

    def test_rollback_invalid_slot(self, tmp_path):
        """回滚到不存在的槽位应失败。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()
        pr = PromoteRollback(manager=mgr)

        result = pr.rollback("C")
        assert result["ok"] is False
        assert "not found" in result["error"].lower()
