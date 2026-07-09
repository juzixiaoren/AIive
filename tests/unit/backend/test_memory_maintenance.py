"""测试记忆维护（MemoryMaintenance）和记忆投影（MemoryProjection）模块。

MemoryMaintenance 负责扫描过期/弱记忆，MemoryProjection 负责将记忆导出为 Markdown/JSON。
"""

from aiive.memory.memory_maintenance import MemoryMaintenance
from aiive.memory.memory_store import MemoryStore
from aiive.memory.projection import MemoryProjection


class TestMemoryScan:
    """测试 MemoryMaintenance 的扫描功能。"""

    def test_scan_returns_candidates(self, db_session):
        """扫描应返回活跃记忆统计及休眠/归档候选项。"""
        store = MemoryStore(db_session)
        store.create(content="strong", lifecycle_state="active", confidence=0.9)
        store.create(content="weak", lifecycle_state="candidate", confidence=0.2)
        db_session.flush()

        result = MemoryMaintenance(db_session).scan()
        assert "total_active" in result
        assert "sleep_candidates" in result
        assert "archive_candidates" in result

    def test_pinned_excluded_from_scan(self, db_session):
        """已固定的记忆不应被列入休眠/归档候选项。"""
        store = MemoryStore(db_session)
        store.create(content="pinned", lifecycle_state="candidate", confidence=0.1, pinned=True)
        db_session.flush()

        result = MemoryMaintenance(db_session).scan()
        # 固定记忆不应出现在休眠/归档候选中
        assert all(mid != "pinned" for mid in result["sleep_ids"])
        assert all(mid != "pinned" for mid in result["archive_ids"])


class TestProjection:
    """测试 MemoryProjection 的记忆导出功能。"""

    def test_markdown_projection(self, db_session):
        """Markdown 投影应包含记忆内容和类型。"""
        store = MemoryStore(db_session)
        store.create(content="User loves coffee", memory_type="preference", lifecycle_state="active")
        store.create(content="User wakes at 7am", memory_type="routine", lifecycle_state="active")
        db_session.flush()

        proj = MemoryProjection(db_session)
        md = proj.to_markdown()
        assert "User loves coffee" in md
        assert "preference" in md

    def test_json_projection(self, db_session):
        """JSON 投影应返回正确的记录数量。"""
        store = MemoryStore(db_session)
        store.create(content="test", lifecycle_state="active")
        db_session.flush()

        proj = MemoryProjection(db_session)
        js = proj.to_json()
        assert len(js) == 1
        assert js[0]["content"] == "test"

    def test_write_projection(self, db_session, tmp_path):
        """写投影应在目标目录生成 memories.md 和 memories.json 文件。"""
        store = MemoryStore(db_session)
        store.create(content="test", lifecycle_state="active")
        db_session.flush()

        proj = MemoryProjection(db_session)
        result = proj.write_projection(tmp_path)
        assert (tmp_path / "memories.md").exists()
        assert (tmp_path / "memories.json").exists()
