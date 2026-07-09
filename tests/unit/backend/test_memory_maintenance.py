from aiive.memory.memory_maintenance import MemoryMaintenance
from aiive.memory.memory_store import MemoryStore
from aiive.memory.projection import MemoryProjection


class TestMemoryScan:
    def test_scan_returns_candidates(self, db_session):
        store = MemoryStore(db_session)
        store.create(content="strong", lifecycle_state="active", confidence=0.9)
        store.create(content="weak", lifecycle_state="candidate", confidence=0.2)
        db_session.flush()

        result = MemoryMaintenance(db_session).scan()
        assert "total_active" in result
        assert "sleep_candidates" in result
        assert "archive_candidates" in result

    def test_pinned_excluded_from_scan(self, db_session):
        store = MemoryStore(db_session)
        store.create(content="pinned", lifecycle_state="candidate", confidence=0.1, pinned=True)
        db_session.flush()

        result = MemoryMaintenance(db_session).scan()
        # Pinned should not be in sleep/archive candidates
        assert all(mid != "pinned" for mid in result["sleep_ids"])
        assert all(mid != "pinned" for mid in result["archive_ids"])


class TestProjection:
    def test_markdown_projection(self, db_session):
        store = MemoryStore(db_session)
        store.create(content="User loves coffee", memory_type="preference", lifecycle_state="active")
        store.create(content="User wakes at 7am", memory_type="routine", lifecycle_state="active")
        db_session.flush()

        proj = MemoryProjection(db_session)
        md = proj.to_markdown()
        assert "User loves coffee" in md
        assert "preference" in md

    def test_json_projection(self, db_session):
        store = MemoryStore(db_session)
        store.create(content="test", lifecycle_state="active")
        db_session.flush()

        proj = MemoryProjection(db_session)
        js = proj.to_json()
        assert len(js) == 1
        assert js[0]["content"] == "test"

    def test_write_projection(self, db_session, tmp_path):
        store = MemoryStore(db_session)
        store.create(content="test", lifecycle_state="active")
        db_session.flush()

        proj = MemoryProjection(db_session)
        result = proj.write_projection(tmp_path)
        assert (tmp_path / "memories.md").exists()
        assert (tmp_path / "memories.json").exists()
