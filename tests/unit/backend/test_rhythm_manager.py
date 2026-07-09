"""测试 RhythmManager（节奏管理器）模块。

验证日报和周报的结构完整性。
"""

from aiive.runtime.rhythm_manager import RhythmManager


class TestRhythmManager:
    """测试 RhythmManager 的摘要生成功能。"""

    def test_daily_summary_returns_structure(self, db_session):
        """日报应返回包含 date、events_today、active_routines、pending_tasks 的结构。"""
        mgr = RhythmManager(db_session)
        summary = mgr.daily_summary()
        assert "date" in summary
        assert "events_today" in summary
        assert "active_routines" in summary
        assert "pending_tasks" in summary

    def test_weekly_summary_returns_structure(self, db_session):
        """周报应返回包含 since、total_events、top_event_types 的结构。"""
        mgr = RhythmManager(db_session)
        summary = mgr.weekly_summary()
        assert "since" in summary
        assert "total_events" in summary
        assert "top_event_types" in summary
