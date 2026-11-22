"""Concurrent scheduling through a real PostgreSQL OutboxWorker."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import event, text

from aiive.db.models import OutboxJob, Thread
from aiive.worker import scheduler_daemon
from aiive.worker.handler_registry import HandlerRegistry
from aiive.worker.outbox_handlers import register_all
from aiive.worker.outbox_heartbeat import ActiveClaimRegistry
from aiive.worker.outbox_worker import OutboxWorker

pytestmark = pytest.mark.pg


@pytest.fixture
def registered_worker(monkeypatch):
    registry = HandlerRegistry()
    register_all(registry)
    worker = OutboxWorker("maintenance-pg", registry, ActiveClaimRegistry())
    monkeypatch.setattr(scheduler_daemon, "_outbox_worker", worker)


@pytest.mark.parametrize("second_window", ["daily", "manual-attempt"])
def test_concurrent_maintenance_enqueue_single_flight(
    pg_sessionmaker, pg_engine, registered_worker, second_window,
):
    waiting = Event()

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if conn.info.get("contender") and "pg_advisory_xact_lock" in statement:
            waiting.set()

    def enqueue_second():
        with pg_sessionmaker() as db:
            info = db.connection().info
            info["contender"] = True
            try:
                db.execute(text("SET LOCAL lock_timeout = '10s'"))
                result = scheduler_daemon.enqueue_maintenance_job(db, window_bucket=second_window)
                db.commit()
                return result
            finally:
                info.pop("contender", None)

    event.listen(pg_engine, "before_cursor_execute", before_execute)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pg_sessionmaker() as first:
                op_id = scheduler_daemon.enqueue_maintenance_job(first, window_bucket="daily")
                contender = pool.submit(enqueue_second)
                try:
                    assert waiting.wait(10)
                finally:
                    first.commit()
            assert contender.result(timeout=10) == (op_id if second_window == "daily" else None)
    finally:
        event.remove(pg_engine, "before_cursor_execute", before_execute)

    with pg_sessionmaker() as db:
        job = db.query(OutboxJob).filter_by(job_type="memory_maintenance").one()
        assert job.operation_id == op_id
        assert job.status == "pending"


def test_enqueue_unique_race_preserves_callers_transaction(
    pg_sessionmaker, pg_engine, registered_worker,
):
    """The UNIQUE fallback handles a producer outside the advisory-lock protocol."""
    injected_job = False

    def insert_from_other_producer(conn, cursor, statement, parameters, context, executemany):
        nonlocal injected_job
        if injected_job or "INSERT INTO outbox_jobs" not in statement:
            return
        injected_job = True
        with pg_sessionmaker() as other:
            operation_id = parameters["operation_id"]
            other.add(OutboxJob(
                operation_id=operation_id, job_type="memory_maintenance",
                status="completed", payload={"operation_id": operation_id},
            ))
            other.commit()

    event.listen(pg_engine, "before_cursor_execute", insert_from_other_producer)
    try:
        with pg_sessionmaker() as db:
            db.add(Thread(id="caller-write"))
            op_id = scheduler_daemon.enqueue_maintenance_job(db, window_bucket="unique-race")
            db.commit()
    finally:
        event.remove(pg_engine, "before_cursor_execute", insert_from_other_producer)

    assert injected_job
    with pg_sessionmaker() as db:
        assert db.get(Thread, "caller-write") is not None
        job = db.query(OutboxJob).filter_by(job_type="memory_maintenance").one()
        assert job.operation_id == op_id and job.status == "completed"
