"""V62.6.1 PostgreSQL regression through production handlers and tool executor."""
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from tests.integration.backend import test_forget_v6261 as cases

pytestmark = pytest.mark.pg


@pytest.mark.parametrize("case", [
    "test_memory_closure_and_missing_manual_proof",
    "test_memory_only_does_not_reflect_to_sibling",
    "test_projection_handlers_zero_calls_from_event_only_shield",
    "test_maintenance_sources_and_frozen_action",
    "test_old_task_time_and_turn_sources",
    "test_evidence_and_mixed_working_state",
    "test_direct_vector_and_index_rebuild_blocked",
    "test_epoch_rollover_filters_mixed_snapshot_and_blocks_unknown",
    "test_bad_manifest_element_is_unresolved",
    "test_maintenance_skips_are_terminal_and_cursor_advances",
    "test_real_manual_api_without_event_remains_visible",
    "test_event_shield_evidence_never_calls_discovery_llm",
    "test_historical_persisted_batch_rechecked_before_mutation",
    "test_epoch_rollover_handler_event_shield_creates_no_snapshot",
    "test_maintenance_clone_inherits_event_sources",
    "test_write_merge_inherits_all_event_sources",
])
def test_pg_source_paths(case, pg_session, pg_engine, monkeypatch, tmp_path):
    from aiive.worker import outbox_execution_auth as auth

    monkeypatch.setattr(auth, "get_engine", lambda: pg_engine)
    values = {"db_session": pg_session, "monkeypatch": monkeypatch, "tmp_path": tmp_path}
    function = getattr(cases, case)
    function(**{name: values[name] for name in inspect.signature(function).parameters})


@pytest.mark.parametrize("restore_fails", [False, True])
def test_pg_agent_task_real_pool_forget(pg_sessionmaker, pg_engine, monkeypatch, tmp_path, restore_fails):
    from aiive.control import capability_broker
    from aiive.task_runtime.agent_executor import TaskAgentExecutor, AgentDecision
    from aiive.task_runtime.repository import TaskRepository
    from aiive.task_runtime.schemas import ActionProposal
    from aiive.tools.registry import ToolRegistry, ToolRegistration, CapabilitySafetySchema
    from aiive.tools.forget_tool import handle_forget
    from aiive.worker import handlers_agent_tasks, outbox_execution_auth as auth
    from aiive.db.forget_models import ForgetOperation
    from tests.integration.pg.test_pg_forget_v626_guard import _running_job

    monkeypatch.setattr(auth, "get_engine", lambda: pg_engine)
    monkeypatch.setattr(handlers_agent_tasks, "SessionLocal", pg_sessionmaker)
    worker_threads = []
    registry = ToolRegistry()

    def forget_handler():
        worker_threads.append(threading.get_ident())
        return handle_forget(mode="history_only", thread_id=thread_id, requested_by=cases.uid())

    registry.register(ToolRegistration(
        CapabilitySafetySchema(capability_id="test_forget", definition_source="local_builtin", definition_trust_level="trusted", risk_level="low", allowed_instruction_sources=["task_runtime"], timeout_seconds=2),
        forget_handler, parameters={},
    ))
    monkeypatch.setattr(capability_broker, "build_registry_for_thread", lambda *a: registry)
    model_threads = []

    def decide(*args, **kwargs):
        model_threads.append(threading.get_ident())
        assert auth.has_execution_authorization()
        assert len(model_threads) == 1, "no inference using forgotten task input"
        return AgentDecision(proposal=ActionProposal(decision="action", capability_id="test_forget"), model="spy", token_usage={}, input_snapshot={}, prompt_refs=[])

    acquire = auth._acquire_shared
    def checked_acquire(*args, **kwargs):
        if restore_fails and model_threads and worker_threads and threading.get_ident() == model_threads[0]:
            raise RuntimeError("test_restore_failed")
        return acquire(*args, **kwargs)
    monkeypatch.setattr(auth, "_acquire_shared", checked_acquire)
    spy = Mock(side_effect=decide)
    monkeypatch.setattr(TaskAgentExecutor, "decide", spy)
    with pg_sessionmaker() as db:
        thread, _, _ = cases.source(db)
        thread_id = thread.id
        task = TaskRepository(db).create_task(thread_id=thread_id, goal="forget test", task_brief={"scope": {"allowed_capabilities": ["test_forget"]}}, enqueue=False)
        db.commit()
        job = _running_job(db, job_type="agent_task_run", payload={"task_id": task.id})
    parent = threading.get_ident()
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(handlers_agent_tasks.handle_agent_task_run, job).result(timeout=15)
    if restore_fails:
        assert "execution_authorization_restore_failed" in result.reason
        second = handlers_agent_tasks.handle_agent_task_run(job)
        assert second.reason.startswith("forget_source_blocked")
    else:
        from aiive.worker.outbox_dto import HandlerOutcome
        assert result.outcome == HandlerOutcome.COMPLETED
        assert result.reason == "forget_source_blocked:task_content_unavailable"
        second = handlers_agent_tasks.handle_agent_task_run(job)
        assert second.outcome == HandlerOutcome.COMPLETED
        assert second.reason.startswith("forget_source_blocked")
    assert spy.call_count == 1
    assert len(worker_threads) == 1 and worker_threads[0] != model_threads[0]
    assert worker_threads[0] != parent
    with pg_sessionmaker() as db:
        from aiive.db.models import TaskEvidence, AgentTaskEvent
        assert db.query(ForgetOperation).count() == 1
        assert db.query(TaskEvidence).filter_by(task_id=task.id).count() == 0
        assert db.query(AgentTaskEvent).filter_by(task_id=task.id, event_type="run_failed").count() == 0


def test_pg_timeout_revokes_late_forget(pg_sessionmaker, pg_engine, monkeypatch):
    from aiive.db.forget_models import ForgetOperation
    from aiive.tools.forget_tool import handle_forget
    from aiive.worker import outbox_execution_auth as auth

    monkeypatch.setattr(auth, "get_engine", lambda: pg_engine)
    with pg_sessionmaker() as db:
        thread, _, _ = cases.source(db)
        thread_id = thread.id
        db.commit()
    ready, proceed = threading.Event(), threading.Event()
    outcomes = []

    def delayed():
        ready.set()
        assert proceed.wait(5)
        outcomes.append(handle_forget(mode="history_only", thread_id=thread_id, requested_by=cases.uid()))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with auth.shared_execution_authorization():
            with pytest.raises(TimeoutError):
                auth.execute_in_tool_thread(pool, delayed, 0.05)
            assert ready.is_set() and auth.has_execution_authorization()
            proceed.set()
    assert outcomes and outcomes[0]["ok"] is False
    with pg_sessionmaker() as db:
        assert db.query(ForgetOperation).count() == 0


def test_pg_started_forget_waits_for_commit(pg_sessionmaker, pg_engine, monkeypatch):
    from aiive.db.forget_models import ForgetOperation
    from aiive.forget.phase_a_shield import execute_phase_a_shield
    from aiive.worker import outbox_execution_auth as auth

    monkeypatch.setattr(auth, "get_engine", lambda: pg_engine)
    with pg_sessionmaker() as db:
        thread, _, _ = cases.source(db)
        thread_id = thread.id
        db.commit()
    entered, commit_allowed = threading.Event(), threading.Event()
    def forget():
        with pg_sessionmaker() as db:
            execute_phase_a_shield(db, mode="history_only", thread_id=thread_id, requested_by=cases.uid())
            entered.set()
            assert commit_allowed.wait(5)
            db.commit()
        return "committed"
    def release_commit():
        assert entered.wait(5)
        # Ensure the generic timeout expires while Phase A owns its transaction.
        threading.Event().wait(0.15)
        commit_allowed.set()
    releaser = threading.Thread(target=release_commit)
    releaser.start()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool, auth.shared_execution_authorization():
            assert auth.execute_in_tool_thread(pool, forget, 0.05) == "committed"
            assert auth.has_execution_authorization()
        with pg_sessionmaker() as db:
            assert db.query(ForgetOperation).count() == 1
    finally:
        commit_allowed.set()
        releaser.join(5)
