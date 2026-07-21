"""提醒生产单轨回归测试：Task → Outbox → Agent Turn → completed。"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from aiive.db.models import Event, OutboxJob, Task, Thread, TurnRecord
from aiive.runtime.task_manager import TaskManager
from aiive.worker.handler_registry import HandlerRegistry
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome
from aiive.worker.outbox_handlers import handle_reminder_delivery, register_all
from aiive.worker.outbox_worker import OutboxWorker
from aiive.worker.task_worker import TaskWorker


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
    return task


def _claim(db, job: OutboxJob) -> ClaimedJob:
    job.status = "running"
    job.locked_by = "test-worker"
    job.claim_token = "test-claim"
    job.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    db.commit()
    return ClaimedJob(
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


def test_worker_only_enqueues_due_reminder(db_session, monkeypatch):
    """旧 TaskWorker 不再直接回复，只原子进入 dispatching 并创建唯一 Job。"""
    monkeypatch.setattr(
        "aiive.worker.task_worker.ThreadBootstrapService.ensure_committed_thread",
        lambda thread_id: thread_id,
    )
    task = _create_due_reminder(db_session)

    results = TaskWorker(db_session).poll_and_notify()
    db_session.expire_all()

    assert results[0]["status"] == "dispatching"
    assert db_session.get(Task, task.id).status == "dispatching"
    jobs = db_session.query(OutboxJob).filter(OutboxJob.job_type == "reminder_delivery").all()
    assert len(jobs) == 1
    assert jobs[0].operation_id == f"reminder_delivery:{task.id}"

    assert TaskWorker(db_session).poll_and_notify() == []
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


def test_reminder_handler_requires_agent_success(db_session, monkeypatch):
    """Agent 真实回复成功后才完成 Task，并广播该真实回复。"""
    import aiive.worker.outbox_handlers as handlers

    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(handlers, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(
        "aiive.worker.task_worker.ThreadBootstrapService.ensure_committed_thread",
        lambda thread_id: thread_id,
    )
    task = _create_due_reminder(db_session, "喝水")
    TaskWorker(db_session).poll_and_notify()
    job = db_session.query(OutboxJob).filter(OutboxJob.job_type == "reminder_delivery").one()
    claimed = _claim(db_session, job)
    fake_result = {
        "reply": "该喝水了。",
        "thread_id": task.thread_id,
        "trace_id": "agent-trace",
        "action_cards": [],
    }
    execute_turn = MagicMock(return_value=fake_result)
    broadcast = MagicMock()
    monkeypatch.setattr(
        "aiive.runtime.turn_execution.TurnExecutionService.execute_turn",
        execute_turn,
    )
    monkeypatch.setattr(
        "aiive.api.ws_manager.ws_manager.broadcast_to_thread_sync",
        broadcast,
    )

    result = handle_reminder_delivery(claimed)
    db_session.expire_all()

    assert result.outcome == HandlerOutcome.COMPLETED
    assert db_session.get(Task, task.id).status == "completed"
    assert execute_turn.call_count == 1
    assert execute_turn.call_args.kwargs["turn_id"]
    assert broadcast.call_args.args[2]["reply"] == "该喝水了。"
    assert db_session.query(Event).filter(Event.event_type == "reminder_triggered").count() == 1
    assert db_session.query(Event).filter(Event.event_type == "notification_created").count() == 0


def test_agent_failure_retries_without_fake_reply(db_session, monkeypatch):
    """Agent 失败时保持 dispatching，不写伪通知也不完成任务。"""
    import aiive.worker.outbox_handlers as handlers

    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(handlers, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(
        "aiive.worker.task_worker.ThreadBootstrapService.ensure_committed_thread",
        lambda thread_id: thread_id,
    )
    task = _create_due_reminder(db_session)
    TaskWorker(db_session).poll_and_notify()
    job = db_session.query(OutboxJob).filter(OutboxJob.job_type == "reminder_delivery").one()
    claimed = _claim(db_session, job)
    monkeypatch.setattr(
        "aiive.runtime.turn_execution.TurnExecutionService.execute_turn",
        lambda *args, **kwargs: {"reply": "", "error": "llm_unavailable"},
    )

    result = handle_reminder_delivery(claimed)
    db_session.expire_all()

    assert result.outcome == HandlerOutcome.RETRYABLE_ERROR
    assert db_session.get(Task, task.id).status == "dispatching"
    assert db_session.query(Event).filter(Event.event_type == "notification_created").count() == 0
    assert db_session.query(TurnRecord).count() == 0


def test_registry_and_deadletter_mark_task_failed(db_session, monkeypatch):
    """生产注册可发现提醒 Handler；重试耗尽后 Task 与 Job 原子失败。"""
    import aiive.worker.outbox_worker as worker_module

    registry = HandlerRegistry()
    register_all(registry)
    assert registry.get("reminder_delivery") is handle_reminder_delivery

    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(worker_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(
        "aiive.worker.task_worker.ThreadBootstrapService.ensure_committed_thread",
        lambda thread_id: thread_id,
    )
    task = _create_due_reminder(db_session)
    task_id = task.id
    TaskWorker(db_session).poll_and_notify()
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
