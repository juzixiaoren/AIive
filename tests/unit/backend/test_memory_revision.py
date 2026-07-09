from aiive.memory.memory_store import MemoryStore


class TestMemoryRevision:
    def test_supersede_moves_old_to_superseded(self, db_session):
        store = MemoryStore(db_session)
        old = store.create(content="用户叫 A", memory_type="user_profile", lifecycle_state="active")
        db_session.flush()

        new = store.supersede(old.id, "用户叫 B")
        db_session.flush()

        old_refetched = store.get_by_id(old.id)
        assert old_refetched.lifecycle_state == "superseded"

        new_refetched = store.get_by_id(new.id)
        assert new_refetched.lifecycle_state == "active"

    def test_superseded_not_in_active(self, db_session):
        store = MemoryStore(db_session)
        old = store.create(content="用户叫 A", lifecycle_state="active")
        db_session.flush()
        store.supersede(old.id, "用户叫 B")
        db_session.flush()

        active = store.get_active()
        assert all(r.id != old.id for r in active)

    def test_update_content(self, db_session):
        store = MemoryStore(db_session)
        rec = store.create(content="old content", lifecycle_state="active")
        db_session.flush()

        store.update_content(rec.id, "new content")
        db_session.flush()

        refetched = store.get_by_id(rec.id)
        assert refetched.content == "new content"

    def test_resolve_for_context_returns_active_only(self, db_session):
        store = MemoryStore(db_session)
        store.create(content="active", lifecycle_state="active")
        store.create(content="superseded", lifecycle_state="superseded")
        db_session.flush()

        resolved = store.resolve_for_context()
        assert len(resolved) == 1
        assert resolved[0].content == "active"
