"""Attachment Forget uses real PostgreSQL commit/rollback and shared-reference checks."""

import pytest
from tests.integration.backend.test_chat_attachment_forget import (
    test_attachment_scrub_verify_and_post_commit_delete as _scrub_case,
    test_shared_object_and_memory_only as _shared_case,
    test_legacy_forgotten_original_is_compensated_and_verified as _compensate_case,
    test_legacy_pending_provenance_repair_and_forgotten_object_compensation as _repair_case,
)
from tests.integration.backend.test_forget_content_coverage import (
    _isolated_object_store,
)  # noqa: F401


@pytest.mark.parametrize(
    "selector", ["event", "turn", "thread", "time_range", "all_user_data"]
)
def test_pg_attachment_commit_and_rollback(pg_session, selector):
    _scrub_case(pg_session, selector)


def test_pg_attachment_shared_object(pg_session):
    _shared_case(pg_session)


def test_pg_attachment_historical_repair(pg_session):
    _repair_case(pg_session)


def test_pg_attachment_compensated_original(pg_session):
    _compensate_case(pg_session)
