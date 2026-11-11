"""V64.2: final injection decisions persist in existing PostgreSQL JSON metadata."""
from tests.integration.backend.test_context_assembler_input_snapshot import (
    test_rotate_snapshot_persists_context_items as _persist,
)
from tests.integration.backend.test_context_v641 import (
    test_only_final_injected_memories_are_touched as _touch,
)


def test_pg_final_selection_controls_touch_and_trace(pg_session, monkeypatch):
    _touch(pg_session, monkeypatch)


def test_pg_selection_trace_uses_existing_snapshot_meta(pg_session):
    _persist(pg_session)
