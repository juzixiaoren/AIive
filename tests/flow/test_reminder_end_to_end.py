"""提醒生产单轨回归测试：Task → Outbox → 通知收件箱。"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from aiive.db.models import Event, OutboxJob, Task, Thread, TurnRecord
from aiive.runtime.task_manager import TaskManager
from aiive.worker.handler_registry import HandlerRegistry
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome
from aiive.worker.outbox_handlers import handle_reminder_delivery, register_all
from aiive.worker.outbox_worker import OutboxWorker
from aiive.worker.task_worker import enqueue_due_tasks
from tests.support import ensure_running_claim


def _create_due_reminder(db, title: str = "hi") -> Task:
    thread = Thread(id="reminder-thread", title="提醒测试")
    db.merge(thread)
    task = TaskManager(db).create(
        task_type="reminder",
        title=title,
        description="测试提醒",
        next_check_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    task.thread_id = thread.id
    db.commit()
    db.add(Event(
        trace_id=task.id,
        thread_id=thread.id,
        event_type="reminder_created",
        payload={
            "task_id": task.id,
            "title": title,
            "content": title,
            "status": "pending",
        },
    ))
    db.commit()
    return task


def _claim(db, job: OutboxJob) -> ClaimedJob:
    job.status = "running"
    job.locked_by = "test-worker"
    job.claim_token = "test-claim"
    job.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    db.commit()
    claimed = ClaimedJob(
        id=job.id,
        job_type=job.job_type,
        payload=dict(job.payload or {}),
        trace_id=job.trace_id,
        retry_count=job.retry_count,
        max_retries=job.max_retries,
        claim_token="test-claim",
        schema_version=1,
        worker_id="test-worker",
    )
    return ensure_running_claim(claimed)


def test_worker_only_enqueues_due_reminder(db_session):
    """到期扫描只原子进入 dispatching 并创建唯一 Job。"""
    task = _create_due_reminder(db_session)

    results = enqueue_due_tasks(db_session)
    db_session.commit()
    db_session.expire_all()

    assert results[0]["status"] == "dispatching"
    assert db_session.get(Task, task.id).status == "dispatching"
    jobs = db_session.query(OutboxJob).filter(OutboxJob.job_type == "reminder_delivery").all()
    assert len(jobs) == 1
    assert jobs[0].operation_id == f"reminder_delivery:{task.id}"

    assert enqueue_due_tasks(db_session) == []
    assert db_session.query(OutboxJob).filter(OutboxJob.job_type == "reminder_delivery").count() == 1


def test_production_scheduler_enqueues_same_delivery_job(db_session, monkeypatch):
    """生产 Scheduler 必须复用同一入队入口，不能直接执行 Agent。"""
    import aiive.worker.scheduler_daemon as scheduler_daemon

    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(scheduler_daemon, "SessionLocal", lambda: db_session)
    add_job = MagicMock()
    monkeypatch.setattr(scheduler_daemon.scheduler, "add_job", add_job)
    task = _create_due_reminder(db_session, "生产路径")

    scheduler_daemon._poll_job()
    db_session.expire_all()

    assert db_session.get(Task, task.id).status == "dispatching"
    assert db_session.query(OutboxJob).filter(
        OutboxJob.operation_id == f"reminder_delivery:{task.id}"
    ).count() == 1
    add_job.assert_called_once()


def test_delivery_is_deterministic_and_idempotent(db_session, monkeypatch):
    import aiive.worker.outbox_handlers as handlers
    import aiive.core.llm_client as llm

    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(handlers, "SessionLocal", lambda: db_session)
    model = MagicMock(side_effect=AssertionError("Reminder delivery must not start a model"))
    monkeypatch.setattr(llm, "default_llm_client", model)
    task = _create_due_reminder(db_session, "stay hydrated")
    enqueue_due_tasks(db_session)
    job = db_session.query(OutboxJob).filter_by(job_type="reminder_delivery").one()
    claimed = _claim(db_session, job)

    assert handle_reminder_delivery(claimed).outcome == HandlerOutcome.COMPLETED
    assert handle_reminder_delivery(claimed).outcome == HandlerOutcome.COMPLETED
    db_session.expire_all()
    assert task.status == "completed"
    reminder = db_session.query(Event).filter_by(trace_id=task.id, event_type="reminder_created").one()
    assert reminder.payload["status"] == "alerting"
    assert reminder.payload["alerted_at"]
    assert db_session.query(TurnRecord).count() == 0
    assert db_session.query(Event).filter_by(event_type="notification_created").count() == 0
    model.assert_not_called()


def test_cancelled_claim_does_not_activate_notification(db_session, monkeypatch):
    import aiive.worker.outbox_handlers as handlers

    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(handlers, "SessionLocal", lambda: db_session)
    task = _create_due_reminder(db_session)
    enqueue_due_tasks(db_session)
    claimed = _claim(db_session, db_session.query(OutboxJob).filter_by(job_type="reminder_delivery").one())
    assert TaskManager(db_session).cancel(task.id)["task_cancelled"]
    db_session.commit()

    assert handle_reminder_delivery(claimed).outcome == HandlerOutcome.COMPLETED
    db_session.expire_all()
    assert task.status == "cancelled"
    event = db_session.query(Event).filter_by(trace_id=task.id, event_type="reminder_created").one()
    assert event.payload["status"] == "cancelled"


def test_legacy_durable_task_gets_one_notification(db_session, monkeypatch):
    import aiive.worker.outbox_handlers as handlers

    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(handlers, "SessionLocal", lambda: db_session)
    task = _create_due_reminder(db_session)
    db_session.query(Event).filter_by(trace_id=task.id, event_type="reminder_created").delete()
    enqueue_due_tasks(db_session)
    claimed = _claim(db_session, db_session.query(OutboxJob).filter_by(job_type="reminder_delivery").one())
    assert handle_reminder_delivery(claimed).outcome == HandlerOutcome.COMPLETED
    event = db_session.query(Event).filter_by(trace_id=task.id, event_type="reminder_created").one()
    assert event.payload["status"] == "alerting"


def test_registry_and_deadletter_mark_task_failed(db_session, monkeypatch):
    """生产注册可发现提醒 Handler；重试耗尽后 Task 与 Job 原子失败。"""
    import aiive.worker.outbox_worker as worker_module

    registry = HandlerRegistry()
    register_all(registry)
    assert registry.get("reminder_delivery") is handle_reminder_delivery

    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(worker_module, "SessionLocal", lambda: db_session)
    task = _create_due_reminder(db_session)
    task_id = task.id
    enqueue_due_tasks(db_session)
    db_session.commit()
    job = db_session.query(OutboxJob).filter(OutboxJob.job_type == "reminder_delivery").one()
    job_id = job.id
    claimed = _claim(db_session, job)

    class _Claims:
        pass

    worker = OutboxWorker("test-worker", registry, _Claims())
    worker._deadletter_job_and_ingestion_run(
        claimed,
        ingestion_run_id="",
        error="Agent 连续失败",
        terminal_reason="max_retries_exhausted",
    )
    db_session.expire_all()

    assert db_session.get(Task, task_id).status == "failed"
    assert db_session.get(OutboxJob, job_id).status == "deadletter"
    assert db_session.query(Event).filter(Event.event_type == "reminder_delivery_failed").count() == 1
    assert db_session.query(Event).filter(Event.event_type == "notification_created").count() == 0
