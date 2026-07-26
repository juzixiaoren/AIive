"""提醒生产单轨回归测试：Task → Outbox → Agent Turn → completed。"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from aiive.db.models import Event, OutboxJob, Task, Thread, TurnRecord
from aiive.runtime.task_manager import TaskManager
from aiive.worker.handler_registry import HandlerRegistry
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome
from aiive.worker.outbox_handlers import handle_reminder_delivery, register_all
from aiive.worker.outbox_worker import OutboxWorker
from aiive.worker.task_worker import enqueue_due_tasks


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


def _fake_execute_with_remind_alert(db_session, reminder_event_id: str):
    """模拟 Agent：真实写入 remind_alert 工具调用与成功结果事件。"""
    def _run(self, message="", thread_id=None, turn_id=None, **kwargs):
        db_session.add(Event(
            trace_id="agent-trace", thread_id=thread_id, turn_id=turn_id,
            turn_event_index=1, event_type="tool_call",
            payload={"name": "remind_alert", "params": {"reminder_id": reminder_event_id},
                     "tool_call_id": "call-alert"},
        ))
        db_session.add(Event(
            trace_id="agent-trace", thread_id=thread_id, turn_id=turn_id,
            turn_event_index=2, event_type="tool_result",
            payload={"name": "remind_alert", "result": {"ok": True}, "status": "completed",
                     "tool_call_id": "call-alert"},
        ))
        db_session.commit()
        return {
            "reply": "该喝水了。",
            "event_id": "assistant-event-1",
            "thread_id": thread_id,
            "trace_id": "agent-trace",
            "action_cards": [],
        }
    return _run


def test_reminder_handler_requires_agent_success(db_session, monkeypatch):
    """Agent 真实调用 remind_alert 成功后，才完成 Task 并广播真实回复。"""
    import aiive.worker.outbox_handlers as handlers

    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(handlers, "SessionLocal", lambda: db_session)
    task = _create_due_reminder(db_session, "喝水")
    enqueue_due_tasks(db_session)
    db_session.commit()
    job = db_session.query(OutboxJob).filter(OutboxJob.job_type == "reminder_delivery").one()
    claimed = _claim(db_session, job)
    reminder_event = db_session.query(Event).filter(
        Event.event_type == "reminder_created", Event.trace_id == task.id,
    ).one()
    execute_turn = _fake_execute_with_remind_alert(db_session, reminder_event.id)
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
    assert broadcast.call_args.args[2]["reply"] == "该喝水了。"
    assert broadcast.call_args.args[2]["event_id"] == "assistant-event-1"
    assert db_session.query(Event).filter(Event.event_type == "reminder_triggered").count() == 1
    reminder_event = db_session.query(Event).filter(
        Event.event_type == "reminder_created",
        Event.trace_id == task.id,
    ).one()
    assert reminder_event.payload["status"] == "alerting"


def test_reminder_handler_requires_remind_alert_call(db_session, monkeypatch):
    """未真实调用 remind_alert 时降级为兜底通知（COMPLETED），不再无效重试。

    旧行为返回 RETRYABLE_ERROR，但 turn_id 是确定性的：重试只会命中已完成
    Turn 的缓存，「未调工具」的状态永远不会改变，最终空转到 deadletter 且用户
    毫无感知。现改为写 notification_created 兜底事件并推进 Task 终态。"""
    import aiive.worker.outbox_handlers as handlers
    from sqlalchemy.orm import sessionmaker

    # Handler 各段使用独立会话（仍绑定测试库），避免测试外层事务连接
    # 被 db_c.rollback() 解除关联导致夹具数据丢失；生产环境本就如此。
    test_session_factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(handlers, "SessionLocal", test_session_factory)
    task = _create_due_reminder(db_session, "喝水")
    task_id = task.id
    enqueue_due_tasks(db_session)
    db_session.commit()
    job = db_session.query(OutboxJob).filter(OutboxJob.job_type == "reminder_delivery").one()
    claimed = _claim(db_session, job)

    def _run_without_alert(self, message="", thread_id=None, turn_id=None, **kwargs):
        # 模拟 Agent 仅回复、未调用 remind_alert
        return {
            "reply": "（假装回复）该喝水了。",
            "event_id": "assistant-event-2",
            "thread_id": thread_id,
            "trace_id": "agent-trace",
            "action_cards": [],
        }

    monkeypatch.setattr(
        "aiive.runtime.turn_execution.TurnExecutionService.execute_turn",
        _run_without_alert,
    )

    result = handle_reminder_delivery(claimed)

    assert result.outcome == HandlerOutcome.COMPLETED
    task_row = db_session.query(Task.id, Task.status).filter(Task.id == task_id).first()
    assert task_row is not None
    assert task_row.status == "completed"
    # 不伪造 reminder_triggered，而是写降级兜底通知
    assert db_session.query(Event).filter(Event.event_type == "reminder_triggered").count() == 0
    fallback = db_session.query(Event).filter(
        Event.event_type == "notification_created",
    ).all()
    assert any((e.payload or {}).get("degraded") for e in fallback)
    reminder_row = db_session.query(Event.id, Event.payload).filter(
        Event.event_type == "reminder_created", Event.trace_id == task_id,
    ).first()
    assert reminder_row is not None
    assert reminder_row.payload["status"] == "alerting"


def test_agent_failure_retries_without_fake_reply(db_session, monkeypatch):
    """Agent 失败时保持 dispatching，不写伪通知也不完成任务。"""
    import aiive.worker.outbox_handlers as handlers

    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(handlers, "SessionLocal", lambda: db_session)
    task = _create_due_reminder(db_session)
    enqueue_due_tasks(db_session)
    db_session.commit()
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
