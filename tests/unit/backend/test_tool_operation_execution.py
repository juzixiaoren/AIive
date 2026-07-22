"""副作用工具持久化 operation、幂等执行和未知终态回归测试。"""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aiive.context.run_context import RunContext
from aiive.db.models import (
    Base,
    Event,
    MemoryProposal,
    MemoryRecord,
    OutboxJob,
    Task,
    Thread,
    ToolOperation,
    TurnRecord,
)
from aiive.tools.operation_executor import enqueue_tool_operation
from aiive.tools.registry import get_tool_registry
from aiive.worker.handler_registry import HandlerRegistry
from aiive.worker.outbox_handlers import register_all
from aiive.worker.outbox_heartbeat import ActiveClaimRegistry
from aiive.worker.outbox_worker import OutboxWorker


@pytest.fixture
def operation_session_factory(tmp_path, monkeypatch):
    """为 operation 与 Worker 提供可跨线程访问的独立 SQLite 数据库。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tool-operation.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("aiive.tools.operation_executor.SessionLocal", factory)
    monkeypatch.setattr("aiive.worker.outbox_worker.SessionLocal", factory)
    monkeypatch.setattr("aiive.tools.builtin_tools.SessionLocal", factory)
    yield factory
    engine.dispose()


def _source_context(factory) -> RunContext:
    """创建满足 ToolOperation 外键约束的真实 Thread 和 TurnRecord。"""
    db = factory()
    try:
        thread = Thread(id=str(uuid.uuid4()), title="operation-test")
        turn = TurnRecord(
            id=str(uuid.uuid4()),
            thread_id=thread.id,
            turn_id=str(uuid.uuid4()),
            status="running",
            request_fingerprint="test",
        )
        db.add_all([thread, turn])
        db.commit()
        return RunContext(
            thread_id=thread.id,
            trace_id=str(uuid.uuid4()),
            turn_id=turn.turn_id,
            turn_record_id=turn.id,
        )
    finally:
        db.close()


def _worker() -> OutboxWorker:
    """构造已注册 tool_operation handler 的真实 OutboxWorker。"""
    handlers = HandlerRegistry()
    register_all(handlers)
    return OutboxWorker("tool-operation-test", handlers, ActiveClaimRegistry())


def test_duplicate_call_reuses_one_persisted_operation(operation_session_factory):
    """同一 Turn/tool_call/参数的重试必须复用 operation，不重复入队。"""
    context = _source_context(operation_session_factory)
    registry = get_tool_registry()
    registration = registry.get("schedule_reminder")
    assert registration is not None

    first = enqueue_tool_operation(
        "schedule_reminder", {"content": "喝水", "delay_minutes": 1},
        registration.safety.descriptor_hash, registration.safety.effect_mode,
        context, "call-stable",
    )
    second = enqueue_tool_operation(
        "schedule_reminder", {"content": "喝水", "delay_minutes": 1},
        registration.safety.descriptor_hash, registration.safety.effect_mode,
        context, "call-stable",
    )

    db = operation_session_factory()
    try:
        assert first.id == second.id
        assert db.query(ToolOperation).count() == 1
        assert db.query(OutboxJob).filter(OutboxJob.job_type == "tool_operation").count() == 1
    finally:
        db.close()


def test_db_side_effect_and_receipt_commit_atomically(operation_session_factory):
    """Worker 成功后业务 Task、committed receipt 和终态 Event 必须同时存在。"""
    context = _source_context(operation_session_factory)
    registry = get_tool_registry()
    registration = registry.get("schedule_reminder")
    assert registration is not None
    operation = enqueue_tool_operation(
        "schedule_reminder", {"content": "喝水", "delay_minutes": 1},
        registration.safety.descriptor_hash, registration.safety.effect_mode,
        context, "call-commit",
    )

    assert _worker().poll(max_jobs=1) == 1

    db = operation_session_factory()
    try:
        persisted = db.get(ToolOperation, operation.id)
        assert persisted is not None
        assert persisted.status == "committed"
        assert persisted.effect_receipt == {"transactional": True, "committed": True}
        assert db.query(Task).filter(Task.title == "喝水").count() == 1
        terminals = db.query(Event).filter(
            Event.event_type == "tool_operation_terminal",
        ).all()
        terminal = next(event for event in terminals if event.payload.get("operation_id") == operation.id)
        assert terminal.payload["status"] == "committed"
    finally:
        db.close()


def test_registry_timeout_returns_unknown_then_worker_commits_once(operation_session_factory):
    """有限等待超时只能返回 unknown，后台最终提交且相同调用不会产生重复副作用。"""
    context = _source_context(operation_session_factory)
    registry = get_tool_registry()
    result = registry.execute(
        "schedule_reminder",
        {"content": "站起来", "delay_minutes": 1},
        "trusted_user_command",
        context,
        tool_call_id="call-timeout",
    )
    assert result["ok"] is False
    assert result["error_type"] == "execution_unknown"
    assert result["tool_call_id"] == "call-timeout"
    operation_id = result["operation_id"]

    assert _worker().poll(max_jobs=1) == 1
    replay = registry.execute(
        "schedule_reminder",
        {"content": "站起来", "delay_minutes": 1},
        "trusted_user_command",
        context,
        tool_call_id="call-timeout",
    )

    db = operation_session_factory()
    try:
        assert replay["ok"] is True
        assert replay["operation_id"] == operation_id
        assert replay["tool_call_id"] == "call-timeout"
        assert db.query(Task).filter(Task.title == "站起来").count() == 1
        assert db.query(ToolOperation).count() == 1
    finally:
        db.close()


def test_remember_or_update_commits_memory_and_receipt_atomically(operation_session_factory):
    """显式记忆必须经真实 Worker 同时提交记忆、提案和 committed receipt。"""
    context = _source_context(operation_session_factory)
    context.source_event_ids = ["source-event-memory"]
    registry = get_tool_registry()
    registration = registry.get("remember_or_update")
    assert registration is not None
    operation = enqueue_tool_operation(
        "remember_or_update",
        {
            "content": "用户偏好深色主题",
            "memory_type": "user_profile",
            "memory_key": "user.preference.theme",
        },
        registration.safety.descriptor_hash,
        registration.safety.effect_mode,
        context,
        "call-remember",
    )

    assert _worker().poll(max_jobs=1) == 1

    db = operation_session_factory()
    try:
        persisted = db.get(ToolOperation, operation.id)
        assert persisted is not None
        assert persisted.status == "committed"
        assert persisted.effect_receipt == {"transactional": True, "committed": True}
        assert db.query(MemoryRecord).filter(
            MemoryRecord.canonical_key == "user.preference.theme",
        ).count() == 1
        proposal = db.query(MemoryProposal).filter(
            MemoryProposal.source_turn_record_id == context.turn_record_id,
        ).one()
        assert proposal.final_memory_id is not None
    finally:
        db.close()


def test_confirm_rejects_non_reminder_event(operation_session_factory):
    """确认工具不得修改非 reminder_created 类型的 Event。"""
    context = _source_context(operation_session_factory)
    db = operation_session_factory()
    try:
        event = Event(
            id=str(uuid.uuid4()),
            trace_id=str(uuid.uuid4()),
            thread_id=context.thread_id,
            event_type="notification_created",
            payload={"status": "alerting", "content": "普通通知"},
        )
        db.add(event)
        db.commit()
        event_id = event.id
    finally:
        db.close()

    registry = get_tool_registry()
    registration = registry.get("confirm_reminder")
    assert registration is not None
    operation = enqueue_tool_operation(
        "confirm_reminder",
        {"reminder_id": event_id},
        registration.safety.descriptor_hash,
        registration.safety.effect_mode,
        context,
        "call-confirm-non-reminder",
    )
    assert _worker().poll(max_jobs=1) == 1

    db = operation_session_factory()
    try:
        persisted = db.get(ToolOperation, operation.id)
        assert persisted is not None
        assert persisted.status == "failed"
        event = db.get(Event, event_id)
        assert event is not None
        assert event.payload["status"] == "alerting"
    finally:
        db.close()


def test_repeated_snooze_creates_one_followup_task(operation_session_factory):
    """同一提醒即使使用不同 tool_call_id 重复延期也只能创建一个后续提醒。"""
    context = _source_context(operation_session_factory)
    db = operation_session_factory()
    try:
        event = Event(
            id=str(uuid.uuid4()),
            trace_id=str(uuid.uuid4()),
            thread_id=context.thread_id,
            event_type="reminder_created",
            payload={"status": "alerting", "content": "喝水"},
        )
        db.add(event)
        db.commit()
        event_id = event.id
    finally:
        db.close()

    registry = get_tool_registry()
    registration = registry.get("snooze_reminder")
    assert registration is not None
    for tool_call_id, delay_minutes in (("call-snooze-1", 5), ("call-snooze-2", 10)):
        enqueue_tool_operation(
            "snooze_reminder",
            {"reminder_id": event_id, "delay_minutes": delay_minutes},
            registration.safety.descriptor_hash,
            registration.safety.effect_mode,
            context,
            tool_call_id,
        )
        assert _worker().poll(max_jobs=1) == 1

    db = operation_session_factory()
    try:
        event = db.get(Event, event_id)
        assert event is not None
        assert event.payload["status"] == "snoozed"
        assert event.payload["snooze_delay_minutes"] == 5
        assert event.payload["snoozed_to"]
        assert db.query(Task).filter(Task.title == "喝水").count() == 1
        followups = db.query(Event).filter(
            Event.event_type == "reminder_created",
        ).all()
        assert len(followups) == 2
        assert db.query(ToolOperation).filter(
            ToolOperation.capability_id == "snooze_reminder",
            ToolOperation.status == "committed",
        ).count() == 2
    finally:
        db.close()
