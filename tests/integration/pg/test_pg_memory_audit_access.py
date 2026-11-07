"""PostgreSQL READ COMMITTED snapshot invalidation and lifecycle checks."""
import pytest
from tests.integration.backend.test_forget_v6261 import source, memory, shield
from aiive.forget.concurrency import forget_revision
from aiive.memory.memory_lifecycle_service import MemoryLifecycleService

pytestmark = pytest.mark.pg


def test_committed_forget_invalidates_prior_snapshot(pg_sessionmaker):
    with pg_sessionmaker() as first, pg_sessionmaker() as second:
        _, _, event = source(first)
        record = memory(first, event)
        record.lifecycle_state = "sleeping"
        first.commit()
        prior = forget_revision(first)
        second_event = second.get(type(event), event.id)
        shield(second, second_event)
        assert forget_revision(first) != prior
        assert not MemoryLifecycleService(first).wake(record.id)
        first.rollback()
