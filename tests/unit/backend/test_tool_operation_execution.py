"""副作用工具持久化 operation、幂等执行和未知终态回归测试。"""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aiive.context.run_context import RunContext
from aiive.db.forget_models import ContentProvenanceRef
from aiive.db.models import (
    Base,
    Event,
    MemoryEvidence,
    MemoryProposal,
    MemoryRecord,
    OutboxJob,
    Task,
    Thread,
    ToolOperation,
    TurnRecord,
)
from aiive.tools.operation_executor import enqueue_tool_operation
from aiive.task_runtime.tools import build_main_agent_registry
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
    monkeypatch.setattr("aiive.db.base.SessionLocal", factory)
    monkeypatch.setattr("aiive.tools.operation_executor.SessionLocal", factory)
    monkeypatch.setattr("aiive.worker.outbox_worker.SessionLocal", factory)
    monkeypatch.setattr("aiive.tools.builtin_tools.SessionLocal", factory)
    yield factory
    engine.dispose()


def _source_context(factory, *, source="user", with_request=True) -> RunContext:
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
            source=source,
        )
        db.add_all([thread, turn])
        if with_request:
            event = Event(id=str(uuid.uuid4()), thread_id=thread.id, turn_id=turn.turn_id,
                          trace_id="", event_type="user_message", payload={"message_source": source})
            turn.request_event_id = event.id
            db.add(event)
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
    main = build_main_agent_registry()
    for name in ("memory_search", "memory_timeline", "remember_or_update", "history_search", "forget", "forget_status"):
        assert main.get(name) is not None
        assert type(main.get(name)) is type(registry.get(name))
        assert main.get(name).safety.descriptor_hash == registry.get(name).safety.descriptor_hash
    registration = main.get("remember_or_update")
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

    with operation_session_factory() as db:
        persisted = db.get(ToolOperation, operation.id)
        assert persisted is not None
        assert persisted.status == "committed"
        assert persisted.effect_receipt == {"transactional": True, "committed": True}
        record = db.query(MemoryRecord).filter(
            MemoryRecord.canonical_key == "user.preference.theme",
        ).one()
        assert persisted.result_payload["ok"] and persisted.result_payload["written"]
        assert persisted.result_payload["state"] == "active"
        proposal = db.query(MemoryProposal).filter(
            MemoryProposal.source_turn_record_id == context.turn_record_id,
        ).one()
        event_id = db.get(TurnRecord, context.turn_record_id).request_event_id
        assert event_id != context.trace_id and event_id not in context.source_event_ids
        assert proposal.source_event_ids == [event_id]
        assert proposal.evidence[0]["source_event_id"] == event_id
        assert proposal.final_memory_id == record.id
        assert db.query(MemoryEvidence).filter_by(memory_id=record.id).one().source_event_id == event_id
        assert db.query(ContentProvenanceRef).filter_by(owner_id=record.id).one().source_id == event_id
        memory_id = record.id

    context.source_event_ids = [event_id]
    def execute(name, args):
        return main.execute(name, args, "trusted_user_command", context)

    search = execute("memory_search", {"query": "user.preference.theme"})
    assert memory_id in {row["memory_id"] for row in search["result"]["results"]}
    timeline = execute("memory_timeline", {"memory_id": memory_id, "include_evidence": True})
    assert timeline["result"]["current"]["evidence"][0]["source_event_id"] == event_id

    from aiive.forget.phase_a_shield import execute_phase_a_shield
    from aiive.runtime.context_assembler import ContextAssembler
    from aiive.runtime.token_counter import LiteLLMTokenCounter

    def automatically_recalled_ids():
        with operation_session_factory() as db:
            assembler = ContextAssembler(LiteLLMTokenCounter())
            assembled = assembler.assemble(db, "user.preference.theme", db.get(Thread, context.thread_id))
            return {item["memory_id"] for item in assembled.snapshot.injected_memory_ids}

    assert memory_id in automatically_recalled_ids()
    with operation_session_factory.begin() as db:
        execute_phase_a_shield(db, mode="history_only", event_ids=[event_id], requested_by="test")
    context.memory_tool_calls = 0
    assert execute("memory_search", {"query": "user.preference.theme"})["result"]["results"] == []
    assert execute("memory_timeline", {"memory_id": memory_id})["ok"] is False
    assert execute("get_tool_operation_status", {"operation_id": operation.id})["ok"] is False
    assert memory_id not in automatically_recalled_ids()


@pytest.mark.parametrize(("decision", "outcome", "written", "state", "audit_operation"), [
    ("candidate", "written", True, "candidate", "create"),
    ("reject", "gate_rejected", False, "", "reject"),
    ("ignore", "ignored", False, "", "ignore"),
    ("replay", "reinforce_skipped", False, "active", "create"),
])
def test_memory_decisions_commit_with_audit(
    operation_session_factory, monkeypatch, decision, outcome, written, state, audit_operation,
):
    from aiive.memory.conflict_resolver import ConflictResolver, ResolutionResult
    from aiive.memory.memory_gate import GateDecision, MemoryGate

    context = _source_context(operation_session_factory)
    tool = get_tool_registry().get("remember_or_update")
    args = {"content": "dark mode", "memory_type": "user_profile", "memory_key": "user.preference.theme"}
    if decision == "ignore":
        monkeypatch.setattr(ConflictResolver, "resolve", lambda *_: ResolutionResult("ignore", reason="test ignore"))
    elif decision != "replay":
        monkeypatch.setattr(MemoryGate, "decide", lambda *_: GateDecision(decision, reason="test decision"))

    def enqueue(call_id):
        return enqueue_tool_operation("remember_or_update", args, tool.safety.descriptor_hash,
                                      tool.safety.effect_mode, context, call_id)

    operation = enqueue("first")
    assert _worker().poll(max_jobs=1) == 1
    if decision == "replay":
        operation = enqueue("repeat")
        assert _worker().poll(max_jobs=10) >= 1

    with operation_session_factory() as db:
        receipt = db.get(ToolOperation, operation.id)
        assert receipt.status == "committed"
        result = receipt.result_payload
        assert (result["ok"], result["written"], result["outcome"], result["state"]) == (True, written, outcome, state)
        proposal = db.query(MemoryProposal).filter_by(source_turn_record_id=context.turn_record_id).one()
        assert proposal.final_operation == audit_operation
        assert result["canonical_key"] == proposal.canonical_key


@pytest.mark.parametrize(("source", "with_request"), [("user", False), ("runtime_event", True), ("system_command", True)])
def test_memory_worker_fails_closed_without_user_event(operation_session_factory, source, with_request):
    context = _source_context(operation_session_factory, source=source, with_request=with_request)
    tool = get_tool_registry().get("remember_or_update")
    operation = enqueue_tool_operation("remember_or_update", {"content": "should not be saved"},
                                       tool.safety.descriptor_hash, tool.safety.effect_mode, context, "invalid-source")
    assert _worker().poll(max_jobs=1) == 1
    with operation_session_factory() as db:
        receipt = db.get(ToolOperation, operation.id)
        assert (receipt.status, receipt.terminal_reason) == ("failed", "invalid_memory_source")
        assert db.query(MemoryRecord).count() == db.query(MemoryProposal).count() == 0


@pytest.mark.parametrize("status", ["queued", "running", "execution_unknown", "failed", "committed"])
def test_get_tool_operation_status_reads_only_same_thread(operation_session_factory, status):
    main = build_main_agent_registry()
    context = _source_context(operation_session_factory)
    other_context = _source_context(operation_session_factory)
    tool = main.get("schedule_reminder")
    operation = enqueue_tool_operation("schedule_reminder", {}, tool.safety.descriptor_hash,
                                       tool.safety.effect_mode, context, "status-test")
    with operation_session_factory.begin() as db:
        persisted = db.get(ToolOperation, operation.id)
        persisted.status = status
        persisted.result_payload = {"confirmed": True}
        persisted.error_message = "test error" if status == "failed" else None
        persisted.terminal_reason = "test terminal reason" if status == "failed" else None

    args = {"operation_id": operation.id}
    response = main.execute("get_tool_operation_status", args, "trusted_user_command", context)["result"]
    assert response["status"] == status
    assert response["result"] == ({"confirmed": True} if status == "committed" else None)
    assert response["error"] == ("test error" if status == "failed" else None)
    assert response["terminal_reason"] == ("test terminal reason" if status == "failed" else None)
    denied = main.execute("get_tool_operation_status", args, "trusted_user_command", other_context)
    missing = main.execute("get_tool_operation_status", {"operation_id": "missing"}, "trusted_user_command", context)
    assert denied["ok"] is False
    assert denied["error_type"] == missing["error_type"] == "operation_not_found"


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


def test_external_failure_persists_side_effect_receipt(operation_session_factory, monkeypatch):
    """外部工具确定性失败但已产生部分副作用时，回执必须保留这个事实。"""
    from aiive.tools.base import BaseTool
    from aiive.tools.registry import CapabilitySafetySchema, ToolDefinition, ToolRegistry

    class PartialFailureTool(BaseTool):
        def _run(self):
            return {
                "ok": False,
                "error_type": "execution_failed",
                "error": "写了一半文件",
                "outcome_known": True,
                "side_effect_started": True,
            }

    registry = ToolRegistry()
    registry.register(PartialFailureTool(ToolDefinition(
        CapabilitySafetySchema(
            "contract_partial_external", "test", "trusted", "medium",
            writes_external_world=True, effect_mode="non_repeatable_external",
        ),
        parameters={},
    )))
    monkeypatch.setattr("aiive.tools.registry.get_tool_registry", lambda: registry)

    context = _source_context(operation_session_factory)
    registration = registry.get("contract_partial_external")
    operation = enqueue_tool_operation(
        "contract_partial_external", {},
        registration.safety.descriptor_hash, registration.safety.effect_mode,
        context, "call-partial",
    )

    assert _worker().poll(max_jobs=1) == 1

    db = operation_session_factory()
    try:
        persisted = db.get(ToolOperation, operation.id)
        assert persisted is not None
        assert persisted.status == "failed"
        assert persisted.effect_receipt == {
            "outcome_known": True, "side_effect_started": True,
        }
    finally:
        db.close()


def test_run_shell_timeout_persists_partial_side_effect_receipt(
    operation_session_factory, tmp_path,
):
    """真实 Shell 超时：文件已写出，Operation 回执必须记录已产生副作用。"""
    context = _source_context(operation_session_factory)
    registry = get_tool_registry()
    registration = registry.get("run_shell")
    assert registration is not None
    operation = enqueue_tool_operation(
        "run_shell",
        {
            "command": 'echo "partial" > partial.txt; sleep 30',
            "cwd": str(tmp_path),
            "timeout_seconds": 1,
        },
        registration.safety.descriptor_hash, registration.safety.effect_mode,
        context, "call-shell-timeout",
    )

    assert _worker().poll(max_jobs=1) == 1

    assert (tmp_path / "partial.txt").read_text(encoding="utf-8").strip() == "partial"
    db = operation_session_factory()
    try:
        persisted = db.get(ToolOperation, operation.id)
        assert persisted is not None
        assert persisted.status == "failed"
        assert persisted.effect_receipt == {
            "outcome_known": True, "side_effect_started": True,
        }
        terminals = db.query(Event).filter(
            Event.event_type == "tool_operation_terminal",
        ).all()
        terminal = next(
            event for event in terminals
            if event.payload.get("operation_id") == operation.id
        )
        assert terminal.payload["status"] == "failed"
    finally:
        db.close()
