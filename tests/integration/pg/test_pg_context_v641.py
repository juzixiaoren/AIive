"""V64.1 contracts on real PostgreSQL, sharing the SQLite acceptance scenarios."""
import pytest

from tests.integration.backend.test_context_v641 import (
    test_history_only_segment_sealing as _history,
    test_summary_overflow_seals_with_deterministic_fallback as _summary,
)


@pytest.mark.parametrize("retry", [False, True])
def test_pg_summary_overflow_commits_fallback(pg_session, monkeypatch, caplog, retry):
    _summary(pg_session, monkeypatch, retry, caplog)


@pytest.mark.parametrize("kind", ["token", "turn32", "other_segment"])
def test_pg_segment_history_token_trigger(pg_session, monkeypatch, kind):
    _history(pg_session, monkeypatch, kind)
