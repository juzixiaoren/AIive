"""Replay Eval runner - executes offline scenarios through the real execution chain.

Tools go through ToolRegistry.execute; memories through MemoryStore.create_record
(the single-source writer); events / outbox jobs / snapshots through the real ORM
models. No LLM, no network. isolated_eval_session gives a throwaway SQLite DB and
redirects aiive.tools.builtin_tools.SessionLocal to it for db-backed tools.
"""
from __future__ import annotations

import contextlib
import shutil
import tempfile
from collections.abc import Generator
from pathlib import Path

from sqlalchemy.orm import Session

from aiive.context.run_context import RunContext
from aiive.eval.models import EvalCaseResult, EvalRunResult, StepResult
from aiive.eval.scenarios import (
    SCENARIOS,
    EventAssertStep,
    EventEmitStep,
    MemoryAssertStep,
    MemoryWriteStep,
    OutboxAssertStep,
    OutboxEmitStep,
    SafeDeleteStep,
    Scenario,
    SnapshotAssertStep,
    SnapshotEmitStep,
    ToolStep,
)
from aiive.tools.registry import get_tool_registry


@contextlib.contextmanager
def isolated_eval_session() -> Generator[Session, None, None]:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as _Session
    from sqlalchemy.orm import sessionmaker

    import aiive.tools.builtin_tools as bt
    from aiive.db import forget_models, models, retention_models
    from aiive.db.models import Base

    # Force registration of all ORM models on the isolated engine.
    _ = (forget_models, models, retention_models)

    tmp = Path(tempfile.mkdtemp(prefix="eval_run_"))
    db_path = tmp / "eval.db"
    engine = create_engine("sqlite:///" + str(db_path))
    Base.metadata.create_all(engine)

    original_sl = bt.SessionLocal
    bt.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = _Session(bind=engine)
    try:
        yield session
        session.commit()
    finally:
        session.close()
        bt.SessionLocal = original_sl
        engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


class ReplayRunner:
    def __init__(self, scenarios: list[Scenario] | None = None):
        self._scenarios: list[Scenario] = scenarios if scenarios is not None else SCENARIOS

    def run(self, session: Session) -> EvalRunResult:
        cases: list[EvalCaseResult] = []
        for scn in self._scenarios:
            assertions: list[StepResult] = []
            scenario_ok = True
            error = ""
            try:
                for step in scn.steps:
                    try:
                        sr = self._exec_step(step, session)
                    except Exception as exc:  # noqa: BLE001 - record step/scenario failure, keep replay going
                        sr = StepResult(ok=False, message=type(exc).__name__ + ": " + str(exc))
                    assertions.append(sr)
                    if not sr.ok:
                        scenario_ok = False
            except Exception as exc:  # noqa: BLE001 - record step/scenario failure, keep replay going
                scenario_ok = False
                error = type(exc).__name__ + ": " + str(exc)
            total = len(scn.steps)
            passed = sum(1 for a in assertions if a.ok)
            cases.append(EvalCaseResult(scn.id, scn.description, scenario_ok, total, passed, error, assertions))
        passed_count = sum(1 for c in cases if c.ok)
        return EvalRunResult(passed=passed_count, failed=len(cases) - passed_count, cases=cases)


    def _exec_step(self, step: object, session: Session) -> StepResult:
        if isinstance(step, ToolStep):
            return self._exec_tool(step, session)
        if isinstance(step, SafeDeleteStep):
            return self._exec_safe_delete(step, session)
        if isinstance(step, MemoryWriteStep):
            return self._exec_memory_write(step, session)
        if isinstance(step, MemoryAssertStep):
            return self._exec_memory_assert(step, session)
        if isinstance(step, EventEmitStep):
            return self._exec_event_emit(step, session)
        if isinstance(step, EventAssertStep):
            return self._exec_event_assert(step, session)
        if isinstance(step, OutboxEmitStep):
            return self._exec_outbox_emit(step, session)
        if isinstance(step, OutboxAssertStep):
            return self._exec_outbox_assert(step, session)
        if isinstance(step, SnapshotEmitStep):
            return self._exec_snapshot_emit(step, session)
        if isinstance(step, SnapshotAssertStep):
            return self._exec_snapshot_assert(step, session)
        return StepResult(ok=False, message="unknown step type: " + type(step).__name__)


    def _exec_tool(self, step: ToolStep, _session: Session) -> StepResult:
        ctx = RunContext(
            thread_id="eval-replay", trace_id="eval-replay",
            turn_id="eval-replay", turn_record_id="eval-replay", source="user_chat",
        )
        reg = get_tool_registry()
        res = reg.execute(step.call, step.args, step.source, run_context=ctx, tool_call_id="eval")
        if res.get("ok") != step.expect_ok:
            return StepResult(ok=False, message="tool " + step.call + ": ok=" + str(res.get("ok")) + " expected=" + str(step.expect_ok))
        if step.expect_error_type is not None and res.get("error_type") != step.expect_error_type:
            return StepResult(ok=False, message="tool " + step.call + ": error_type mismatch")
        if step.result_contains is not None and step.result_contains not in str(res):
            return StepResult(ok=False, message="tool " + step.call + ": result missing " + repr(step.result_contains))
        return StepResult(ok=True, message="tool " + step.call + " ok=" + str(res.get("ok")))


    def _exec_safe_delete(self, step: SafeDeleteStep, _session: Session) -> StepResult:
        import tempfile

        from aiive.tools.safe_delete import (
            REPO_ROOT,
            SafeDeleteScopeRegistry,
            safe_delete,
        )

        if step.dangerous:
            target = str(Path(REPO_ROOT) / "backend" / "never_deleted_marker.txt")
            res = safe_delete(target, "eval_guard")
        else:
            tmp = Path(tempfile.mkdtemp(prefix="eval_sd_"))
            try:
                reg = SafeDeleteScopeRegistry()
                reg.register("eval_sd", tmp)
                if step.expect_allowed:
                    f = tmp / step.path
                    f.write_text("permitted", encoding="utf-8")
                    res = safe_delete(str(f), "eval_sd", mode=step.mode, registry=reg)
                else:
                    res = safe_delete(str(tmp.parent / step.path), "eval_sd", registry=reg)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        if res.allowed != step.expect_allowed:
            return StepResult(ok=False, message="safe_delete(" + step.path + "): allowed=" + str(res.allowed) + " expected=" + str(step.expect_allowed))
        return StepResult(ok=True, message="safe_delete allowed=" + str(res.allowed))


    def _exec_memory_write(self, step: MemoryWriteStep, session: Session) -> StepResult:
        from aiive.memory.memory_store import MemoryStore
        from aiive.memory.memory_types import (
            EvidenceItem,
            LifecycleState,
            MemoryProposal,
            TrustLevel,
        )
        p = MemoryProposal(
            memory_type=step.memory_type,
            canonical_key=step.canonical_key,
            content=step.content,
            evidence=[EvidenceItem(source_type="user_message", trust_level=TrustLevel.TRUSTED.value)],
        )
        p.compute_request_idempotency()
        store = MemoryStore(session)
        store.create_record(p, lifecycle_state=LifecycleState.ACTIVE.value)
        session.flush()
        return StepResult(ok=True, message="memory written key=" + step.canonical_key)


    def _exec_memory_assert(self, step: MemoryAssertStep, session: Session) -> StepResult:
        from aiive.db.models import MemoryRecord
        from aiive.memory.memory_types import LifecycleState, ValidityState
        rec: object | None = (
            session.query(MemoryRecord)
            .filter(
                MemoryRecord.canonical_key == step.canonical_key,
                MemoryRecord.lifecycle_state == LifecycleState.ACTIVE.value,
                MemoryRecord.validity_state == ValidityState.VALID.value,
            )
            .first()
        )
        exists = rec is not None
        if exists != step.exists:
            return StepResult(ok=False, message="memory key=" + step.canonical_key + " exists=" + str(exists) + " expected=" + str(step.exists))
        return StepResult(ok=True, message="memory assert ok exists=" + str(exists))


    def _exec_event_emit(self, step: EventEmitStep, session: Session) -> StepResult:
        import uuid

        from aiive.db.models import Event
        session.add(Event(
            id=str(uuid.uuid4()), trace_id="eval", thread_id=step.thread_id,
            event_type=step.event_type, payload={}, turn_id=step.turn_id,
        ))
        session.flush()
        return StepResult(ok=True, message="event emitted " + step.event_type)


    def _exec_event_assert(self, step: EventAssertStep, session: Session) -> StepResult:
        from aiive.db.models import Event
        cnt = session.query(Event).filter(Event.event_type == step.event_type).count()
        if cnt < 1:
            return StepResult(ok=False, message="no event of type " + step.event_type)
        return StepResult(ok=True, message="event assert ok count=" + str(cnt))


    def _exec_outbox_emit(self, step: OutboxEmitStep, session: Session) -> StepResult:
        import uuid

        from aiive.db.models import OutboxJob
        op_id = step.operation_id or (step.job_type + ":" + str(uuid.uuid4()))
        session.add(OutboxJob(operation_id=op_id, job_type=step.job_type, status="pending", payload={}))
        session.flush()
        return StepResult(ok=True, message="outbox emitted " + step.job_type)


    def _exec_outbox_assert(self, step: OutboxAssertStep, session: Session) -> StepResult:
        from aiive.db.models import OutboxJob
        cnt = session.query(OutboxJob).filter(OutboxJob.job_type == step.job_type).count()
        if cnt < 1:
            return StepResult(ok=False, message="no outbox job of type " + step.job_type)
        return StepResult(ok=True, message="outbox assert ok count=" + str(cnt))


    def _exec_snapshot_emit(self, step: SnapshotEmitStep, session: Session) -> StepResult:
        import uuid

        from aiive.db.models import ContextSnapshot
        session.add(ContextSnapshot(
            id=str(uuid.uuid4()), trace_id=step.trace_id, thread_id=step.thread_id,
            stable_prefix_hash="eval", context_items=[], meta={},
        ))
        session.flush()
        return StepResult(ok=True, message="snapshot emitted " + step.thread_id)


    def _exec_snapshot_assert(self, step: SnapshotAssertStep, session: Session) -> StepResult:
        from aiive.db.models import ContextSnapshot
        cnt = session.query(ContextSnapshot).filter(ContextSnapshot.thread_id == step.thread_id).count()
        if cnt < 1:
            return StepResult(ok=False, message="no snapshot for thread " + step.thread_id)
        return StepResult(ok=True, message="snapshot assert ok count=" + str(cnt))


def run_replay_isolated() -> EvalRunResult:
    """Run the full replay eval against a fresh isolated DB. Fail-closed on crash."""
    try:
        with isolated_eval_session() as session:
            return ReplayRunner().run(session)
    except Exception as exc:  # noqa: BLE001 - fail-closed: surface any crash as a replay error
        return EvalRunResult(error=type(exc).__name__ + ": " + str(exc))
