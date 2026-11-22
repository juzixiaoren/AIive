"""PostgreSQL row-lock contract for Forget and durable tool completion."""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from aiive.context.run_context import RunContext
from aiive.db.forget_models import ForgetOperation, ForgetSelectorManifest, ForgetTombstone
from aiive.db.models import OutboxJob, Thread, ToolOperation, TurnRecord, WorkingState
from aiive.runtime.agent_graph import _ws_commit
from aiive.runtime.working_state import WorkingStateService
from aiive.tools import operation_executor
from aiive.tools.builtin_runtime_state import UpdateWorkingStateTool
from aiive.tools.registry import get_tool_registry
from aiive.worker import outbox_execution_auth
from aiive.worker.handler_registry import HandlerRegistry
from aiive.worker.handlers_forget import _scrub_wide
from aiive.worker.outbox_handlers import register_all
from aiive.worker.outbox_heartbeat import ActiveClaimRegistry
from aiive.worker.outbox_worker import OutboxWorker

pytestmark = pytest.mark.pg


def test_forget_waits_on_tool_operation_before_locking_working_state(
    pg_sessionmaker, pg_engine, monkeypatch,
):
    monkeypatch.setattr(outbox_execution_auth, "get_engine", lambda: pg_engine)
    now = datetime.now(timezone.utc)
    thread_id, turn_id, turn_record_id, forget_id = [str(uuid.uuid4()) for _ in range(4)]
    ctx = RunContext(thread_id=thread_id, turn_id=turn_id, turn_record_id=turn_record_id, trace_id=turn_id)
    call_id = "update-objective"
    tool_name = "update_working_state"
    working = WorkingStateService()
    with pg_sessionmaker() as db:
        db.add(Thread(id=thread_id))
        db.flush()
        db.add(TurnRecord(
            id=turn_record_id, thread_id=thread_id, turn_id=turn_id, turn_sequence=1,
            request_fingerprint="test", status="running", source="user",
        ))
        working.add_running_tool(db, thread_id, turn_record_id, "exec", call_id, tool_name)
        working.add_uncommitted_side_effect(db, thread_id, call_id, tool_name, "low")
        # Enter at Purge with a scoped manifest so Phase A's separate advisory
        # lock cannot serialize away the physical row-lock interleaving.
        db.add(ForgetOperation(
            id=forget_id, operation_key=f"forget:{forget_id}", mode="everywhere",
            selector_type="thread", selector_hash="test", status="shielded",
            requested_by="test", shielded_at=now,
        ))
        db.flush()
        db.add(ForgetSelectorManifest(
            forget_operation_id=forget_id, selector_type="thread", selector_payload_hash="test",
            selector_payload={"selector_type": "thread", "thread_id": thread_id},
            cutoff_created_at=now + timedelta(minutes=5),
        ))
        db.commit()

    registration = get_tool_registry().get(tool_name)
    assert isinstance(registration, UpdateWorkingStateTool)
    operation = operation_executor.enqueue_tool_operation(
        tool_name, {"field": "current_objective", "payload": {"value": "private objective"}},
        registration.safety.descriptor_hash, registration.safety.effect_mode, ctx, call_id,
    )

    reached_tool = threading.Event()
    reached_purge = threading.Event()
    resume_tool = threading.Event()
    scrubbed = threading.Event()
    commit_purge = threading.Event()
    pids: dict[str, int] = {}
    original_run = UpdateWorkingStateTool._run

    def pause_before_working_state(tool, db, ctx, **kwargs):
        db.execute(text("SET LOCAL lock_timeout = '15s'"))
        pids["tool"] = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
        reached_tool.set()
        assert resume_tool.wait(15)
        return original_run(tool, db, ctx, **kwargs)

    monkeypatch.setattr(UpdateWorkingStateTool, "_run", pause_before_working_state)
    registry = HandlerRegistry()
    register_all(registry)
    worker = OutboxWorker("tool-pg", registry, ActiveClaimRegistry())

    def purge():
        with pg_sessionmaker() as db:
            db.execute(text("SET LOCAL lock_timeout = '15s'"))
            pids["purge"] = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
            reached_purge.set()
            counts = _scrub_wide(db, forget_id)
            db.add(ForgetTombstone(
                forget_operation_id=forget_id, target_type="turn_record",
                target_id=turn_record_id, content_purged=True,
            ))
            scrubbed.set()
            assert commit_purge.wait(15)
            db.commit()
            return counts

    with ThreadPoolExecutor(max_workers=2) as pool:
        tool_future = pool.submit(worker.poll, 1, frozenset({"tool_operation"}))
        try:
            assert reached_tool.wait(10)
            assert operation_executor.wait_for_tool_operation(operation.id, 0)["error_type"] == "execution_unknown"
            purge_future = pool.submit(purge)
            assert reached_purge.wait(10)
            assert pids["tool"] != pids["purge"]
            deadline = time.monotonic() + 8
            with pg_engine.connect() as observer:
                while time.monotonic() < deadline:
                    blockers = observer.execute(text("SELECT pg_blocking_pids(:pid)"), {"pid": pids["purge"]}).scalar_one()
                    if pids["tool"] in blockers:
                        break
                    time.sleep(0.02)
                else:
                    pytest.fail("Purge did not reach the ToolOperation row lock")
            resume_tool.set()
            assert scrubbed.wait(10)
            # The ToolOperation transaction committed while Purge was blocked;
            # Purge's clearing updates are still uncommitted in the other session.
            with pg_sessionmaker() as observer:
                persisted = observer.get(ToolOperation, operation.id)
                ws = observer.query(WorkingState).filter_by(thread_id=thread_id).one()
                assert persisted.status == "committed"
                assert ws.current_objective == "private objective"
                assert ws.running_tool_state == ws.uncommitted_side_effects == []
                assert [item["state"] for item in ws.verified_tool_states] == [
                    {"ok": True, "tool_call_id": call_id, "operation_id": operation.id},
                ]
        finally:
            resume_tool.set()
            commit_purge.set()

        assert tool_future.result(timeout=10) == 1
        counts = purge_future.result(timeout=10)
        assert counts["tool_operations"] == counts["working_states"] == 1

    # A post-commit notification and delayed graph write must not restore any
    # of the forgotten WorkingState entries.
    operation_executor._notify(operation.id)

    def stale_finalizer(db):
        pytest.fail("A forgotten tool result reached the graph WorkingState writer")

    _ws_commit(stale_finalizer, turn_record_id, operation.id)

    with pg_sessionmaker() as db:
        persisted = db.get(ToolOperation, operation.id)
        assert persisted.status == "committed"
        assert persisted.params == persisted.effect_receipt == {}
        assert persisted.result_payload is None and persisted.error_message is None
        assert db.get(OutboxJob, operation.outbox_job_id).status == "completed"
        ws = db.query(WorkingState).filter_by(thread_id=thread_id).one()
        assert ws.current_objective is None
        assert ws.running_tool_state == ws.uncommitted_side_effects == ws.verified_tool_states == []
