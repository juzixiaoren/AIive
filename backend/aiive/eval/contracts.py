"""Kernel Contract Checks - the "brakes" for self-modification.

Each check is a pure function (Session) -> ContractResult asserting one invariant
the execution chain must never violate. Fail-closed: any ok=False or exception
blocks promotion.

Invariants:
  1. memory_single_source_of_truth
  2. key_events_persisted
  3. outbox_idempotent
  4. safe_delete_guard
  5. context_snapshot_persisted
"""
from __future__ import annotations

import logging
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aiive.eval.models import ContractResult

logger = logging.getLogger(__name__)


def check_memory_single_source(session: Session) -> ContractResult:
    from aiive.db.models import MemoryRecord
    from aiive.memory.memory_types import LifecycleState, ValidityState

    rows = (
        session.query(MemoryRecord.canonical_key)
        .filter(
            MemoryRecord.lifecycle_state == LifecycleState.ACTIVE.value,
            MemoryRecord.validity_state == ValidityState.VALID.value,
            MemoryRecord.canonical_key.isnot(None),
        )
        .all()
    )
    counts = Counter(r[0] for r in rows)
    dupes = {k: v for k, v in counts.items() if v > 1}
    ok = not dupes
    return ContractResult(
        name="memory_single_source_of_truth",
        ok=ok,
        severity="critical",
        detail=(
            "every active memory has a unique canonical_key"
            if ok
            else "duplicate active canonical_key detected: " + str(dupes)
        ),
        evidence={"active_memory_count": len(rows), "duplicate_keys": dupes},
    )


def check_key_events_persisted(session: Session) -> ContractResult:
    from aiive.db.models import Event, TurnRecord

    missing = []
    for turn in session.query(TurnRecord).filter(TurnRecord.status == "completed").all():
        has_end = (
            session.query(Event.id)
            .filter(Event.event_type == "chat_ended", Event.turn_id == turn.turn_id)
            .first()
            is not None
        )
        if not has_end:
            missing.append(turn.turn_id)
    ok = not missing
    return ContractResult(
        name="key_events_persisted",
        ok=ok,
        severity="critical",
        detail=(
            "all completed turns have a chat_ended event"
            if ok
            else "completed turns without a chat_ended event: " + str(missing)
        ),
        evidence={"turns_missing_terminal_event": missing},
    )


def check_outbox_idempotent(session: Session) -> ContractResult:
    from aiive.db.models import OutboxJob

    live = (
        session.query(OutboxJob.operation_id)
        .filter(OutboxJob.status.in_(["pending", "running"]))
        .all()
    )
    counts = Counter(r[0] for r in live)
    dupes = {k: v for k, v in counts.items() if v > 1}
    data_ok = not dupes

    probe_ok = False
    try:
        session.add(
            OutboxJob(operation_id="__eval_idem_probe__", job_type="eval_probe", status="pending")
        )
        session.flush()
        session.add(
            OutboxJob(operation_id="__eval_idem_probe__", job_type="eval_probe", status="pending")
        )
        try:
            session.flush()
            probe_ok = False
        except IntegrityError:
            probe_ok = True
        finally:
            session.rollback()
    except Exception:
        logger.exception("outbox idempotency probe failed")
        session.rollback()

    ok = data_ok and probe_ok
    return ContractResult(
        name="outbox_idempotent",
        ok=ok,
        severity="critical",
        detail=(
            "outbox operation_id is unique and re-enqueue is rejected"
            if ok
            else "outbox idempotency violated"
        ),
        evidence={"duplicate_operation_ids": dupes, "idempotency_probe_ok": probe_ok},
    )


def check_safe_delete_guard(_session: Session) -> ContractResult:
    from aiive.tools.safe_delete import SafeDeleteScopeRegistry, safe_delete

    tmp = Path(tempfile.mkdtemp(prefix="eval_sd_"))
    try:
        registry = SafeDeleteScopeRegistry()
        registry.register("eval_guard", tmp)
        in_scope_file = tmp / "allowed.txt"
        in_scope_file.write_text("permitted content", encoding="utf-8")

        in_scope = safe_delete(str(in_scope_file), "eval_guard", mode="trash", registry=registry)
        dangerous = safe_delete("/etc/passwd", "eval_guard", registry=registry)
        out_of_scope = safe_delete(
            str(tmp.parent / "escape.txt"), "eval_guard", registry=registry
        )

        ok = (
            in_scope.allowed is True
            and dangerous.allowed is False
            and out_of_scope.allowed is False
        )
        return ContractResult(
            name="safe_delete_guard",
            ok=ok,
            severity="critical",
            detail=(
                "in-scope deletes allowed; dangerous / out-of-scope deletes refused"
                if ok
                else "safe_delete guard allowed a forbidden deletion"
            ),
            evidence={
                "in_scope_allowed": in_scope.allowed,
                "dangerous_allowed": dangerous.allowed,
                "out_of_scope_allowed": out_of_scope.allowed,
            },
        )
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def check_context_snapshot_persisted(session: Session) -> ContractResult:
    from aiive.db.models import ContextSnapshot, TurnRecord

    missing = []
    for turn in session.query(TurnRecord).all():
        has_snapshot = (
            session.query(ContextSnapshot.id)
            .filter(ContextSnapshot.thread_id == turn.thread_id)
            .first()
            is not None
        )
        if not has_snapshot:
            missing.append(turn.turn_id)
    ok = not missing
    return ContractResult(
        name="context_snapshot_persisted",
        ok=ok,
        severity="warning",
        detail=(
            "every turn has an associated context snapshot"
            if ok
            else "turns without a context snapshot: " + str(missing)
        ),
        evidence={"turns_missing_snapshot": missing},
    )


KERNEL_CONTRACTS: list[Callable[[Session], ContractResult]] = [
    check_memory_single_source,
    check_key_events_persisted,
    check_outbox_idempotent,
    check_safe_delete_guard,
    check_context_snapshot_persisted,
]


def run_contract_checks(session: Session, only: list[str] | None = None) -> list[ContractResult]:
    results = []
    selected = [c for c in KERNEL_CONTRACTS if only is None or c.__name__ in only]
    for check in selected:
        try:
            results.append(check(session))
        except Exception as exc:
            logger.exception("contract check %s raised", check.__name__)
            results.append(
                ContractResult(
                    name=check.__name__,
                    ok=False,
                    severity="critical",
                    detail="contract check raised: " + str(exc),
                    evidence={"exception": type(exc).__name__},
                )
            )
    return results
