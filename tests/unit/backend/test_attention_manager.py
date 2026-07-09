"""测试 AttentionManager 注意力管理功能。"""
from aiive.runtime.attention_manager import AttentionManager


class TestAttentionManager:
    """测试注意力状态的计算、记录和查询。"""

    def test_recompute_returns_continue_for_new_thread(self, db_session):
        """验证新线程首次计算返回 continue 决策。"""
        mgr = AttentionManager(db_session)
        result = mgr.recompute("thread-new", "coding")
        assert result["decision"] == "continue"
        assert result["focus_topic"] == "coding"

    def test_get_current_returns_latest(self, db_session):
        """验证查询当前状态返回最新一条记录。"""
        mgr = AttentionManager(db_session)
        mgr.recompute("thread-1", "topic1")
        db_session.flush()
        mgr.recompute("thread-1", "topic2")
        db_session.flush()

        state = mgr.get_current("thread-1")
        assert state is not None
        assert state.focus_topic == "topic2"

    def test_recompute_records_recent_topics(self, db_session):
        """验证计算结果中包含最近主题列表。"""
        mgr = AttentionManager(db_session)
        result = mgr.recompute("thread-x", "test")
        assert "recent_topics" in result
