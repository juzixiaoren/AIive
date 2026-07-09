"""测试 MemoryStore（记忆存储）模块的 CRUD 操作。

覆盖创建、检索、状态更新、过滤查询及默认值验证。
"""

from aiive.memory.memory_store import MemoryStore


class TestMemoryStore:
    """测试 MemoryStore 的基础增删改查操作。"""

    def test_create_and_retrieve(self, db_session):
        """创建记忆后应能通过 ID 完整检索。"""
        store = MemoryStore(db_session)
        record = store.create(content="User loves coffee", memory_type="preference")
        db_session.flush()

        retrieved = store.get_by_id(record.id)
        assert retrieved is not None
        assert retrieved.content == "User loves coffee"
        assert retrieved.memory_type == "preference"
        assert retrieved.lifecycle_state == "candidate"

    def test_update_state(self, db_session):
        """update_state 应将生命周期状态更新为指定值。"""
        store = MemoryStore(db_session)
        record = store.create(content="Test")
        db_session.flush()

        updated = store.update_state(record.id, "active")
        db_session.flush()
        assert updated.lifecycle_state == "active"

    def test_get_active_returns_only_active(self, db_session):
        """get_active 应仅返回 lifecycle_state 为 active 的记录。"""
        store = MemoryStore(db_session)
        a = store.create(content="Active", lifecycle_state="active")
        store.create(content="Candidate", lifecycle_state="candidate")
        db_session.flush()

        active = store.get_active()
        assert len(active) == 1
        assert active[0].id == a.id

    def test_list_all_returns_all(self, db_session):
        """list_all 应返回所有记忆记录，不区分状态。"""
        store = MemoryStore(db_session)
        store.create(content="A", lifecycle_state="active")
        store.create(content="B", lifecycle_state="candidate")
        db_session.flush()

        all_records = store.list_all()
        assert len(all_records) == 2

    def test_default_values(self, db_session):
        """验证创建记忆时的默认值：类型为 fact，状态为 candidate，置信度 0.5，未固定。"""
        store = MemoryStore(db_session)
        record = store.create(content="Test")
        db_session.flush()

        assert record.memory_type == "fact"
        assert record.lifecycle_state == "candidate"
        assert record.confidence == 0.5
        assert record.pinned is False

    def test_source_event_id_preserved(self, db_session):
        """source_event_id 应在创建后正确保留。"""
        store = MemoryStore(db_session)
        record = store.create(
            content="Test", source_event_id="evt-123"
        )
        db_session.flush()
        assert record.source_event_id == "evt-123"
