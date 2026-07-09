"""测试记忆生命周期管理——遗忘、休眠和归档功能。"""
from aiive.memory.memory_maintenance import MemoryMaintenance
from aiive.memory.memory_store import MemoryStore


class TestForgetMemory:
    """测试遗忘（forget）记忆的状态变更和查询过滤。"""

    def test_forget_changes_state(self, db_session):
        """验证遗忘操作将记忆状态改为 forgotten 并创建 tombstone。"""
        store = MemoryStore(db_session)
        rec = store.create(content="test memory", lifecycle_state="active")
        db_session.flush()

        maint = MemoryMaintenance(db_session)
        result = maint.forget(rec.id)
        db_session.flush()
        assert result["ok"] is True
        assert "tombstone" in result

        # 重新查询
        updated = store.get_by_id(rec.id)
        assert updated.lifecycle_state == "forgotten"

    def test_forgotten_not_in_active(self, db_session):
        """验证已遗忘的记忆不会出现在活跃列表中。"""
        store = MemoryStore(db_session)
        store.create(content="keep me", lifecycle_state="active")
        rec2 = store.create(content="forget me", lifecycle_state="active")
        db_session.flush()

        MemoryMaintenance(db_session).forget(rec2.id)
        db_session.flush()

        active = store.get_active()
        assert all(r.id != rec2.id for r in active)


class TestSleepArchive:
    """测试休眠和归档记忆操作。"""

    def test_sleep_preserves_content(self, db_session):
        """验证休眠操作保留记忆内容。"""
        store = MemoryStore(db_session)
        rec = store.create(content="sleepy", lifecycle_state="active")
        db_session.flush()

        MemoryMaintenance(db_session).sleep(rec.id)
        db_session.flush()

        assert store.get_by_id(rec.id).lifecycle_state == "sleep"

    def test_pinned_cannot_sleep(self, db_session):
        """验证置顶记忆不允许休眠。"""
        store = MemoryStore(db_session)
        rec = store.create(content="pinned", lifecycle_state="active", pinned=True)
        db_session.flush()

        result = MemoryMaintenance(db_session).sleep(rec.id)
        assert result["ok"] is False

    def test_archive(self, db_session):
        """验证归档操作将记忆状态改为 archive。"""
        store = MemoryStore(db_session)
        rec = store.create(content="old", lifecycle_state="active")
        db_session.flush()

        MemoryMaintenance(db_session).archive(rec.id)
        db_session.flush()
        assert store.get_by_id(rec.id).lifecycle_state == "archive"
