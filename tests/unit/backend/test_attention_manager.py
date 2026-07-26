"""测试 AttentionManager 注意力管理功能。"""
from datetime import datetime, timedelta, timezone

from aiive.db.models import AttentionState, Event
from aiive.runtime.attention_manager import AttentionManager


class TestAttentionManager:
    """测试注意力状态的计算、记录和查询。"""

    def test_resolve_for_turn_returns_continue_for_new_thread(self, db_session):
        """验证新线程首回合返回 continue 决策并建立焦点。"""
        mgr = AttentionManager(db_session)
        result = mgr.resolve_for_turn("thread-new", "turn-1", "coding")
        assert result["decision"] == "continue"
        assert result["focus_topic"] == "coding"

    def test_get_current_returns_latest(self, db_session):
        """验证查询当前状态返回最新一条记录。"""
        db_session.add(AttentionState(
            thread_id="thread-1", focus_topic="topic1",
            recent_topics=["topic1"], decision="continue",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        ))
        db_session.add(AttentionState(
            thread_id="thread-1", focus_topic="topic2",
            recent_topics=["topic2"], decision="continue",
            created_at=datetime.now(timezone.utc),
        ))
        db_session.flush()

        state = AttentionManager(db_session).get_current("thread-1")
        assert state is not None
        assert state.focus_topic == "topic2"

    def test_resolve_for_turn_records_recent_topics(self, db_session):
        """验证计算结果中包含最近主题列表。"""
        mgr = AttentionManager(db_session)
        result = mgr.resolve_for_turn("thread-x", "turn-1", "test")
        assert result["recent_topics"] == ["test"]

    def test_resolve_for_turn_initializes_focus_once(self, db_session):
        """首回合建立焦点，连续回合不重复写入状态。"""
        mgr = AttentionManager(db_session)

        first = mgr.resolve_for_turn("thread-focus", "turn-1", "实现注意力接线")
        db_session.flush()
        second = mgr.resolve_for_turn("thread-focus", "turn-2", "继续实现")
        db_session.flush()

        assert first["focus_topic"] == "实现注意力接线"
        assert second["decision"] == "continue"
        assert db_session.query(AttentionState).filter_by(thread_id="thread-focus").count() == 1

    def test_resolve_for_turn_persists_soft_switch_and_recovery(self, db_session):
        """跨软阈值和恢复连续对话均应形成一次状态转换。"""
        now = datetime.now(timezone.utc)
        db_session.add(AttentionState(
            thread_id="thread-soft",
            focus_topic="旧焦点",
            recent_topics=["旧焦点"],
            decision="continue",
        ))
        db_session.add(Event(
            trace_id="trace-soft",
            thread_id="thread-soft",
            turn_id="turn-old",
            event_type="user_message",
            payload={"content": "旧消息"},
            created_at=now - timedelta(hours=5),
        ))
        db_session.flush()
        mgr = AttentionManager(db_session)

        suspended = mgr.resolve_for_turn("thread-soft", "turn-current", "恢复工作")
        db_session.flush()
        db_session.query(Event).filter_by(turn_id="turn-old").update({
            Event.created_at: datetime.now(timezone.utc),
        })
        resumed = mgr.resolve_for_turn("thread-soft", "turn-next", "继续工作")
        db_session.flush()

        assert suspended["decision"] == "suspend"
        assert resumed["decision"] == "continue"
        assert db_session.query(AttentionState).filter_by(thread_id="thread-soft").count() == 3

    def test_resolve_for_turn_hard_switch_updates_focus(self, db_session):
        """跨硬阈值时应以当前回合主题替换持久焦点。"""
        now = datetime.now(timezone.utc)
        db_session.add(AttentionState(
            thread_id="thread-hard",
            focus_topic="旧焦点",
            recent_topics=["旧焦点"],
            decision="continue",
        ))
        db_session.add(Event(
            trace_id="trace-hard",
            thread_id="thread-hard",
            turn_id="turn-old",
            event_type="user_message",
            payload={"content": "旧消息"},
            created_at=now - timedelta(hours=13),
        ))
        db_session.flush()

        result = AttentionManager(db_session).resolve_for_turn(
            "thread-hard", "turn-current", "新的工作重点",
        )

        assert result["decision"] == "switch"
        assert result["focus_topic"] == "新的工作重点"
        assert "长时间间隔" in AttentionManager.render_for_context(result)
