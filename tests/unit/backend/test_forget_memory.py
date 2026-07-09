from aiive.memory.memory_maintenance import MemoryMaintenance
from aiive.memory.memory_store import MemoryStore


class TestForgetMemory:
    def test_forget_changes_state(self, db_session):
        store = MemoryStore(db_session)
        rec = store.create(content="test memory", lifecycle_state="active")
        db_session.flush()

        maint = MemoryMaintenance(db_session)
        result = maint.forget(rec.id)
        db_session.flush()
        assert result["ok"] is True
        assert "tombstone" in result

        # Re-fetch
        updated = store.get_by_id(rec.id)
        assert updated.lifecycle_state == "forgotten"

    def test_forgotten_not_in_active(self, db_session):
        store = MemoryStore(db_session)
        store.create(content="keep me", lifecycle_state="active")
        rec2 = store.create(content="forget me", lifecycle_state="active")
        db_session.flush()

        MemoryMaintenance(db_session).forget(rec2.id)
        db_session.flush()

        active = store.get_active()
        assert all(r.id != rec2.id for r in active)


class TestSleepArchive:
    def test_sleep_preserves_content(self, db_session):
        store = MemoryStore(db_session)
        rec = store.create(content="sleepy", lifecycle_state="active")
        db_session.flush()

        MemoryMaintenance(db_session).sleep(rec.id)
        db_session.flush()

        assert store.get_by_id(rec.id).lifecycle_state == "sleep"

    def test_pinned_cannot_sleep(self, db_session):
        store = MemoryStore(db_session)
        rec = store.create(content="pinned", lifecycle_state="active", pinned=True)
        db_session.flush()

        result = MemoryMaintenance(db_session).sleep(rec.id)
        assert result["ok"] is False

    def test_archive(self, db_session):
        store = MemoryStore(db_session)
        rec = store.create(content="old", lifecycle_state="active")
        db_session.flush()

        MemoryMaintenance(db_session).archive(rec.id)
        db_session.flush()
        assert store.get_by_id(rec.id).lifecycle_state == "archive"
