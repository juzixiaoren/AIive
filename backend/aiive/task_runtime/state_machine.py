"""Task/Run/Action 的确定性状态转换守卫。"""
from __future__ import annotations

from collections.abc import Mapping

from aiive.task_runtime.schemas import ActionStatus, RunStatus, TaskStatus


class InvalidStateTransition(ValueError):
    pass


TASK_TRANSITIONS: Mapping[str, frozenset[str]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.DISPATCHING, TaskStatus.CANCELLED}),
    TaskStatus.DISPATCHING: frozenset({TaskStatus.RUNNING, TaskStatus.QUEUED, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset({
        TaskStatus.QUEUED, TaskStatus.BLOCKED_APPROVAL, TaskStatus.BLOCKED_USER,
        TaskStatus.BLOCKED_NODE, TaskStatus.RECONCILING, TaskStatus.VERIFYING,
        TaskStatus.FAILED, TaskStatus.CANCELLED,
    }),
    TaskStatus.BLOCKED_APPROVAL: frozenset({TaskStatus.QUEUED, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.BLOCKED_USER: frozenset({TaskStatus.QUEUED, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.BLOCKED_NODE: frozenset({TaskStatus.QUEUED, TaskStatus.RECONCILING, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.RECONCILING: frozenset({TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.VERIFYING: frozenset({TaskStatus.SUCCEEDED, TaskStatus.PARTIAL, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.PARTIAL: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

RUN_TRANSITIONS: Mapping[str, frozenset[str]] = {
    RunStatus.CREATED: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.INTERRUPTED}),
    RunStatus.RUNNING: frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.INTERRUPTED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.INTERRUPTED: frozenset(),
}

ACTION_TRANSITIONS: Mapping[str, frozenset[str]] = {
    ActionStatus.PLANNED: frozenset({ActionStatus.VALIDATED, ActionStatus.FAILED, ActionStatus.CANCELLED}),
    ActionStatus.VALIDATED: frozenset({ActionStatus.AWAITING_APPROVAL, ActionStatus.READY, ActionStatus.INVALIDATED, ActionStatus.FAILED, ActionStatus.CANCELLED}),
    ActionStatus.AWAITING_APPROVAL: frozenset({ActionStatus.READY, ActionStatus.CANCELLED, ActionStatus.INVALIDATED}),
    ActionStatus.READY: frozenset({ActionStatus.DISPATCHED, ActionStatus.INVALIDATED, ActionStatus.CANCELLED}),
    ActionStatus.DISPATCHED: frozenset({ActionStatus.RUNNING, ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.UNKNOWN, ActionStatus.INVALIDATED}),
    ActionStatus.RUNNING: frozenset({ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.UNKNOWN}),
    ActionStatus.UNKNOWN: frozenset({ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.READY, ActionStatus.CANCELLED}),
    ActionStatus.SUCCEEDED: frozenset(),
    ActionStatus.FAILED: frozenset(),
    ActionStatus.CANCELLED: frozenset(),
    ActionStatus.INVALIDATED: frozenset(),
}


def ensure_transition(kind: str, current: str, target: str) -> None:
    table = {"task": TASK_TRANSITIONS, "run": RUN_TRANSITIONS, "action": ACTION_TRANSITIONS}.get(kind)
    if table is None:
        raise ValueError(f"unknown state machine: {kind}")
    if target == current:
        return
    if target not in table.get(current, frozenset()):
        raise InvalidStateTransition(f"invalid {kind} transition: {current} -> {target}")
