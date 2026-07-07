from aiive.memory.memory_store import MemoryStore


class TestMemoryStore:
    def test_create_and_retrieve(self, db_session):
        store = MemoryStore(db_session)
        record = store.create(content="User loves coffee", memory_type="preference")
        db_session.flush()

        retrieved = store.get_by_id(record.id)
        assert retrieved is not None
        assert retrieved.content == "User loves coffee"
        assert retrieved.memory_type == "preference"
        assert retrieved.lifecycle_state == "candidate"

    def test_update_state(self, db_session):
        store = MemoryStore(db_session)
        record = store.create(content="Test")
        db_session.flush()

        updated = store.update_state(record.id, "active")
        db_session.flush()
        assert updated.lifecycle_state == "active"

    def test_get_active_returns_only_active(self, db_session):
        store = MemoryStore(db_session)
        a = store.create(content="Active", lifecycle_state="active")
        store.create(content="Candidate", lifecycle_state="candidate")
        db_session.flush()

        active = store.get_active()
        assert len(active) == 1
        assert active[0].id == a.id

    def test_list_all_returns_all(self, db_session):
        store = MemoryStore(db_session)
        store.create(content="A", lifecycle_state="active")
        store.create(content="B", lifecycle_state="candidate")
        db_session.flush()

        all_records = store.list_all()
        assert len(all_records) == 2

    def test_default_values(self, db_session):
        store = MemoryStore(db_session)
        record = store.create(content="Test")
        db_session.flush()

        assert record.memory_type == "fact"
        assert record.lifecycle_state == "candidate"
        assert record.confidence == 0.5
        assert record.pinned is False

    def test_source_event_id_preserved(self, db_session):
        store = MemoryStore(db_session)
        record = store.create(
            content="Test", source_event_id="evt-123"
        )
        db_session.flush()
        assert record.source_event_id == "evt-123"
