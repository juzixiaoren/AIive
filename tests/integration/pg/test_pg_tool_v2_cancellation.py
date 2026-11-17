"""Cancellation races against real Outbox execution and PostgreSQL transactions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading

import pytest

from aiive.db.models import Task, ToolOperation, TurnRecord
from aiive.tools.base import DatabaseTool, CancellationPolicy
from aiive.tools.operation_executor import (
    enqueue_tool_operation,
    request_turn_cancellation,
)
from aiive.tools.registry import CapabilitySafetySchema, ToolDefinition, ToolRegistry
from tests.unit.backend.test_tool_operation_execution import _source_context, _worker

pytestmark = pytest.mark.pg


@pytest.mark.parametrize("cancel_during_execution", [False, True])
def test_turn_cancel_stops_effects_through_real_worker(
    pg_engine, pg_sessionmaker, monkeypatch, cancel_during_execution
):
    from aiive.worker import outbox_execution_auth as auth

    monkeypatch.setattr(auth, "get_engine", lambda: pg_engine)
    for module in (
        "aiive.db.base",
        "aiive.tools.operation_executor",
        "aiive.tools.builtin_tools",
        "aiive.worker.outbox_worker",
    ):
        monkeypatch.setattr(module + ".SessionLocal", pg_sessionmaker)
    started, release = threading.Event(), threading.Event()

    class TransactionalProbe(DatabaseTool):
        def _run(self, db):
            db.add(Task(title="cancel-probe", status="pending", task_type="reminder"))
            db.flush()
            started.set()
            assert release.wait(10)
            return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        TransactionalProbe(
            ToolDefinition(
                CapabilitySafetySchema(
                    "probe", "test", "trusted", "low", writes_external_world=True
                ),
                parameters={},
                cancellation_policy=CancellationPolicy.TRANSACTIONAL,
            )
        )
    )
    monkeypatch.setattr("aiive.tools.registry.get_tool_registry", lambda: registry)
    run = _source_context(pg_sessionmaker)
    tool = registry.get("probe")
    operation = enqueue_tool_operation(
        "probe", {}, tool.safety.descriptor_hash, tool.safety.effect_mode, run, "call"
    )

    def cancel():
        with pg_sessionmaker() as db:
            turn = (
                db.query(TurnRecord)
                .filter_by(id=run.turn_record_id)
                .with_for_update()
                .one()
            )
            now = datetime.now(timezone.utc)
            turn.cancel_requested_at = now
            # Must commit while the worker holds the operation row lock.
            request_turn_cancellation(db, turn.id, now)
            db.commit()

    with ThreadPoolExecutor(1) as pool:
        if cancel_during_execution:
            future = pool.submit(_worker().poll, max_jobs=1)
            assert started.wait(10)
            cancel()
            release.set()
            assert future.result(10) == 1
        else:
            cancel()
            assert _worker().poll(max_jobs=1) == 1
            assert not started.is_set()
    with pg_sessionmaker() as db:
        assert db.get(ToolOperation, operation.id).status == "cancelled"
        assert db.query(Task).filter_by(title="cancel-probe").count() == 0


def test_task_cancel_reaches_dispatched_action(pg_engine, pg_sessionmaker, monkeypatch):
    from aiive.db.models import AgentAction, AgentRun, AgentTask
    from aiive.tools.base import BaseTool
    from aiive.task_runtime.repository import TaskRepository
    from aiive.task_runtime.runtime import PersistentTaskRuntime
    from aiive.task_runtime.schemas import ActionStatus
    from aiive.worker import outbox_execution_auth as auth
    from tests.unit.backend.test_persistent_task_runtime import _thread, _task

    monkeypatch.setattr(auth, "get_engine", lambda: pg_engine)
    monkeypatch.setattr("aiive.db.base.SessionLocal", pg_sessionmaker)
    started = threading.Event()

    class CooperativeProbe(BaseTool):
        def _run(self, _execution=None):
            started.set()
            assert _execution.cancellation._event.wait(10)
            _execution.cancellation.checkpoint()

    registry = ToolRegistry()
    registry.register(
        CooperativeProbe(
            ToolDefinition(
                CapabilitySafetySchema("probe", "test", "trusted", "low"),
                parameters={},
                cancellation_policy=CancellationPolicy.COOPERATIVE,
            )
        )
    )
    monkeypatch.setattr(
        "aiive.control.capability_broker.build_registry_for_thread",
        lambda *args: registry,
    )
    with pg_sessionmaker() as db:
        repo = TaskRepository(db)
        task = _task(
            db, _thread(db), task_brief={"scope": {"allowed_capabilities": ["probe"]}}
        )
        task, run = repo.start_run(task.id, trigger="dispatch")
        action = repo.create_action(
            task=task,
            run=run,
            capability_id="probe",
            arguments={},
            descriptor_hash=registry.get("probe").safety.descriptor_hash,
            risk_level="low",
            requires_approval=False,
        )
        repo.transition_action(action, ActionStatus.VALIDATED.value)
        repo.transition_action(action, ActionStatus.READY.value)
        db.commit()
        task_id, run_id, action_id = task.id, run.id, action.id

    def dispatch():
        with auth.shared_execution_authorization(), pg_sessionmaker() as db:
            runtime = PersistentTaskRuntime(db)
            return runtime._dispatch_ready(
                db.get(AgentTask, task_id),
                db.get(AgentRun, run_id),
                db.get(AgentAction, action_id),
            )

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(dispatch)
        assert started.wait(10)
        with pg_sessionmaker() as db:
            TaskRepository(db).cancel_task(task_id)
            db.commit()
        future.result(10)
    with pg_sessionmaker() as db:
        assert db.get(AgentTask, task_id).status == "cancelled"
        action = db.get(AgentAction, action_id)
        assert action.status == "cancelled"
        assert action.cancel_requested_at is not None
