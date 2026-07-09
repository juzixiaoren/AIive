from aiive.runtime.rhythm_manager import RhythmManager


class TestRhythmManager:
    def test_daily_summary_returns_structure(self, db_session):
        mgr = RhythmManager(db_session)
        summary = mgr.daily_summary()
        assert "date" in summary
        assert "events_today" in summary
        assert "active_routines" in summary
        assert "pending_tasks" in summary

    def test_weekly_summary_returns_structure(self, db_session):
        mgr = RhythmManager(db_session)
        summary = mgr.weekly_summary()
        assert "since" in summary
        assert "total_events" in summary
        assert "top_event_types" in summary
