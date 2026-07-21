"""副作用工具持久化 operation、幂等执行和未知终态回归测试。"""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aiive.context.run_context import RunContext
from aiive.db.models import Base, Event, OutboxJob, Task, Thread, ToolOperation, TurnRecord
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
        assert db.query(Task).filter(Task.title == "站起来").count() == 1
        assert db.query(ToolOperation).count() == 1
    finally:
        db.close()
