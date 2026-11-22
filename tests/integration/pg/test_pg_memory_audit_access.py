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


def test_work_read_query_budgets_on_postgres(pg_sessionmaker):
    from time import perf_counter

    from sqlalchemy import event

    from aiive.task_runtime.work_read_model import WorkReadModel
    from tests.integration.backend.test_work_inbox_api import seed_provenance

    with pg_sessionmaker() as setup:
        seed_provenance(setup, 500)
        setup.commit()
    for name, read, budget in [
        ("Inbox", lambda model: model.inbox(), 100),
        ("WorkView", lambda model: model.work_view("00000"), 100),
        ("Timeline", lambda model: model.timeline("00000"), 30),
        ("Timeline history", lambda model: model.timeline_history("00000"), 30),
    ]:
        with pg_sessionmaker() as reader:
            statements = []

            def capture(conn, cursor, statement, parameters, context, executemany):
                statements.append(statement)

            event.listen(reader.connection(), "before_cursor_execute", capture)
            started = perf_counter()
            result = read(WorkReadModel(reader))
            print(f"PostgreSQL work-read n=500 {name}: {len(statements)} SQL {(perf_counter() - started)*1000:.1f} ms")
            assert result["timeline"] if name == "WorkView" else result["items"]
            assert 0 < len(statements) <= budget
            assert {sql.split()[0].upper() for sql in statements} == {"SELECT"}


def test_work_view_retries_on_postgres_when_forget_commits(pg_sessionmaker):
    from sqlalchemy import event

    from aiive.db.forget_models import ContentProvenanceRef
    from aiive.db.models import AgentTaskEvent, Event
    from aiive.task_runtime.work_read_model import WorkReadModel
    from tests.integration.backend.test_forget_goal_read_bypasses import forget, signal
    from tests.integration.backend.test_work_inbox_api import seed

    with pg_sessionmaker() as setup:
        seed(setup, 1)
        origin = signal(setup, "thread").id
        setup.add_all([
            AgentTaskEvent(id="private", task_id="00000", sequence=1, event_type="task_created"),
            ContentProvenanceRef(owner_type="agent_task_event", owner_id="private",
                                 source_type="event", source_id=origin),
        ])
        setup.commit()
    with pg_sessionmaker() as reader, pg_sessionmaker() as writer:
        revisions = 0

        def capture(conn, cursor, statement, parameters, context, executemany):
            nonlocal revisions
            if "count(forget_operations.id)" in statement.lower():
                revisions += 1
                if revisions == 2:
                    forget(writer, writer.get(Event, origin))

        event.listen(reader.connection(), "before_cursor_execute", capture)
        result = WorkReadModel(reader).work_view("00000")
        assert result["timeline"] == []
        assert result["visibility_revision"] == 1
        assert revisions == 4
