"""事件收件箱溢出与决策串行化（Fake LLM，全离线）。

覆盖：50 条 pending 溢出后淘汰最老事件并记录可识别原因、被淘汰事件的决策作业
在前台 Turn 活跃期间不调用 LLM、Turn 结束后只决策一次、无可淘汰时新事件仍能
恢复、handled 事件不重复处理、缺失收件箱行同样让出、重试间隔对齐租约。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest
from aiive.db.models import AgentTask, Thread, TurnEventInbox, TurnRecord
from aiive.runtime.turn_events import (
    DECISION_EVICTED_OVERFLOW,
    MAX_PENDING_PER_THREAD,
    OPEN_STATUSES,
    enqueue_turn_event,
    inbox_state,
)
from aiive.task_runtime.goal_service import GoalService
from aiive.task_runtime.repository import TaskRepository
from aiive.worker.handlers_goal_triggers import (
    BUSY_RETRY_SECONDS,
    FOREGROUND_WAIT_MAX_SECONDS,
    handle_goal_trigger_decision,
)
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


def _thread(db: Session) -> Thread:
    thread = Thread(id=str(uuid.uuid4()), title="turn event overflow")
    db.add(thread)
    db.flush()
    return thread


def _running_turn(db: Session, thread: Thread) -> TurnRecord:
    turn = TurnRecord(
        thread_id=thread.id,
        turn_id=str(uuid.uuid4()),
        turn_sequence=1,
        status="running",
        execution_id="exec-overflow",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        request_fingerprint="fp",
        source="user",
    )
    db.add(turn)
    db.commit()
    return turn


def _end_turn(db: Session, turn: TurnRecord) -> None:
    db.query(TurnRecord).filter(TurnRecord.id == turn.id).update({
        TurnRecord.status: "completed",
        TurnRecord.lease_expires_at: None,
    })
    db.commit()


def _plan() -> dict[str, Any]:
    return {
        "summary": "溢出测试",
        "timezone": "Asia/Shanghai",
        "acceptance": ["完成"],
        "steps": [
            {
                "id": "s1",
                "title": "等待事件",
                "kind": "autonomous",
                "acceptance": ["完成"],
                "trigger": {"kind": "event_condition", "event_types": ["task_completed"]},
            },
        ],
    }


def _confirmed_goal(db: Session, thread: Thread) -> AgentTask:
    task = TaskRepository(db).create_task(
        thread_id=thread.id,
        goal="溢出测试",
        title="溢出测试",
        task_type="long_term_goal",
        task_brief={"scope": {"allowed_capabilities": ["get_current_time"]}},
        enqueue=False,
    )
    GoalService(db).attach_plan(task, _plan())
    GoalService(db).confirm(task.id)
    db.commit()
    return task


def _arm_step(db: Session, task: AgentTask, trigger_key: str) -> str:
    plan = GoalService(db).load_plan(task)
    assert plan is not None
    step = plan.steps[0]
    step.last_trigger_key = trigger_key
    GoalService(db).save_plan(task, plan)
    db.commit()
    return step.id


def _enqueue(
    db: Session, thread: Thread, *, key: str, index: int, priority: int = 50,
) -> TurnEventInbox:
    row = enqueue_turn_event(
        db,
        thread_id=thread.id,
        event_type="goal_trigger_fired",
        source="goal_trigger",
        dedupe_key=key,
        priority=priority,
        payload={"reason": f"event-{index}"},
    )
    assert row is not None
    # 显式错开创建时间，使“最老事件”在测试中确定。
    row.created_at = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=index)
    db.flush()
    return row


def _fill_and_overflow(db: Session, thread: Thread, *, keys: list[str]) -> str:
    """填满收件箱并再入队一条，返回被淘汰事件的 dedupe_key。"""
    rows = [_enqueue(db, thread, key=key, index=index) for index, key in enumerate(keys)]
    db.commit()
    oldest_key = rows[0].dedupe_key
    _enqueue(db, thread, key="evt-overflow-new", index=len(keys))
    db.commit()
    return oldest_key


def _job(task: AgentTask, step_id: str, trigger_key: str, pending_key: str) -> ClaimedJob:
    return ClaimedJob(
        id=str(uuid.uuid4()),
        job_type="goal_trigger_decision",
        payload={
            "task_id": task.id,
            "step_id": step_id,
            "trigger_key": trigger_key,
            "reason": "事件到达",
            "fired_at": datetime.now(UTC).isoformat(),
            "pending_event_dedupe_key": pending_key,
        },
        trace_id="trace-overflow",
        retry_count=0,
        max_retries=3,
        claim_token=str(uuid.uuid4()),
        schema_version=1,
        worker_id="test",
    )


class _RecordingDecision:
    """记录模型调用次数的 no_action 决策替身。"""

    calls: ClassVar[list[str]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    def context(self, task: Any, plan: Any, step: Any, trigger: Any) -> dict[str, Any]:
        del task, plan, step, trigger
        return {}

    def messages(self, context: Any) -> list[dict[str, str]]:
        del context
        return [{"role": "system", "content": "no_action"}]

    def decide(self, messages: Any, trace_id: str = "") -> tuple[Any, dict[str, str]]:
        del messages
        type(self).calls.append(trace_id)
        from aiive.task_runtime.goal_decision import GoalDecision

        return GoalDecision(action="no_action"), {"model": "fake", "raw": "{}"}

    def apply(self, **kwargs: Any) -> dict[str, str]:
        del kwargs
        return {"status": "applied", "action": "no_action"}

    def write_trace(self, **kwargs: Any) -> None:
        del kwargs


class _FailingDecision:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    def decide(self, messages: Any, trace_id: str = "") -> None:
        del messages, trace_id
        raise AssertionError("前台 Turn 活跃时不得调用模型决策")


# ---------------------------------------------------------------------------
# 1. 溢出淘汰与可识别原因
# ---------------------------------------------------------------------------


def test_overflow_evicts_oldest_pending_with_reason(db_session: Session) -> None:
    thread = _thread(db_session)
    _running_turn(db_session, thread)
    keys = [f"evt-{index:02d}" for index in range(MAX_PENDING_PER_THREAD)]
    oldest_key = _fill_and_overflow(db_session, thread, keys=keys)

    db_session.expire_all()
    evicted = db_session.query(TurnEventInbox).filter(
        TurnEventInbox.dedupe_key == oldest_key,
    ).one()
    assert evicted.status == "expired"
    assert evicted.decision == DECISION_EVICTED_OVERFLOW

    open_rows = db_session.query(TurnEventInbox).filter(
        TurnEventInbox.thread_id == thread.id,
        TurnEventInbox.status.in_(OPEN_STATUSES),
    ).count()
    assert open_rows == MAX_PENDING_PER_THREAD
    assert inbox_state(db_session, "evt-overflow-new") == "pending"


def test_nothing_evictable_rejects_new_event_without_touching_rows(db_session: Session) -> None:
    thread = _thread(db_session)
    _running_turn(db_session, thread)
    for index in range(MAX_PENDING_PER_THREAD):
        _enqueue(db_session, thread, key=f"evt-{index:02d}", index=index)
    db_session.commit()
    # 全部转成 injected：没有 pending 可淘汰。
    db_session.query(TurnEventInbox).filter(
        TurnEventInbox.thread_id == thread.id,
    ).update({TurnEventInbox.status: "injected"})
    db_session.commit()

    rejected = enqueue_turn_event(
        db_session,
        thread_id=thread.id,
        event_type="goal_trigger_fired",
        source="goal_trigger",
        dedupe_key="evt-rejected",
        payload={"reason": "overflow"},
    )
    db_session.commit()

    assert rejected is None
    assert inbox_state(db_session, "evt-rejected") == ""
    assert db_session.query(TurnEventInbox).filter(
        TurnEventInbox.thread_id == thread.id,
        TurnEventInbox.status == "injected",
    ).count() == MAX_PENDING_PER_THREAD


# ---------------------------------------------------------------------------
# 2. 溢出后的决策串行化
# ---------------------------------------------------------------------------


def test_evicted_event_job_defers_while_turn_active(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = _thread(db_session)
    turn = _running_turn(db_session, thread)
    task = _confirmed_goal(db_session, thread)
    step_id = _arm_step(db_session, task, "key-evicted")
    keys = [f"evt-{index:02d}" for index in range(MAX_PENDING_PER_THREAD)]
    oldest_key = _fill_and_overflow(db_session, thread, keys=keys)
    assert inbox_state(db_session, oldest_key) == "expired"

    _RecordingDecision.calls = []
    monkeypatch.setattr(
        "aiive.worker.handlers_goal_triggers.GoalDecisionEngine", _FailingDecision,
    )

    result = handle_goal_trigger_decision(_job(task, step_id, "key-evicted", oldest_key))
    assert result.outcome == HandlerOutcome.RETRY_LATER
    assert result.reason == "foreground_turn_active"
    assert _RecordingDecision.calls == []

    # 重试时间对齐租约但受上限约束：5 分钟租约 → 60s 后再试，不空转刷 Outbox。
    retry_at = result.retry_available_at
    assert retry_at is not None
    now = datetime.now(UTC)
    assert retry_at > now + timedelta(seconds=BUSY_RETRY_SECONDS - 1)
    assert retry_at <= now + timedelta(seconds=FOREGROUND_WAIT_MAX_SECONDS + 1)

    # 短租约：重试点贴到租约到期之后，Turn 一结束就能接手。
    short_lease = datetime.now(UTC) + timedelta(seconds=20)
    db_session.query(TurnRecord).filter(TurnRecord.id == turn.id).update({
        TurnRecord.lease_expires_at: short_lease,
    })
    db_session.commit()
    short = handle_goal_trigger_decision(_job(task, step_id, "key-evicted", oldest_key))
    assert short.outcome == HandlerOutcome.RETRY_LATER
    assert short.retry_available_at is not None
    assert short.retry_available_at >= short_lease


def test_evicted_event_decides_once_after_turn_ends(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = _thread(db_session)
    turn = _running_turn(db_session, thread)
    task = _confirmed_goal(db_session, thread)
    step_id = _arm_step(db_session, task, "key-after")
    keys = [f"evt-{index:02d}" for index in range(MAX_PENDING_PER_THREAD)]
    oldest_key = _fill_and_overflow(db_session, thread, keys=keys)

    _RecordingDecision.calls = []
    monkeypatch.setattr(
        "aiive.worker.handlers_goal_triggers.GoalDecisionEngine", _RecordingDecision,
    )
    deferred = handle_goal_trigger_decision(_job(task, step_id, "key-after", oldest_key))
    assert deferred.outcome == HandlerOutcome.RETRY_LATER
    assert _RecordingDecision.calls == []

    _end_turn(db_session, turn)
    completed = handle_goal_trigger_decision(_job(task, step_id, "key-after", oldest_key))
    assert completed.outcome == HandlerOutcome.COMPLETED
    assert len(_RecordingDecision.calls) == 1


def test_rejected_event_recovers_after_turn_ends(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = _thread(db_session)
    turn = _running_turn(db_session, thread)
    task = _confirmed_goal(db_session, thread)
    step_id = _arm_step(db_session, task, "key-rejected")
    for index in range(MAX_PENDING_PER_THREAD):
        _enqueue(db_session, thread, key=f"evt-{index:02d}", index=index)
    db_session.commit()
    db_session.query(TurnEventInbox).filter(
        TurnEventInbox.thread_id == thread.id,
    ).update({TurnEventInbox.status: "injected"})
    db_session.commit()
    rejected = enqueue_turn_event(
        db_session,
        thread_id=thread.id,
        event_type="goal_trigger_fired",
        source="goal_trigger",
        dedupe_key="evt-rejected",
        payload={"reason": "overflow"},
    )
    db_session.commit()
    assert rejected is None

    _RecordingDecision.calls = []
    monkeypatch.setattr(
        "aiive.worker.handlers_goal_triggers.GoalDecisionEngine", _FailingDecision,
    )
    deferred = handle_goal_trigger_decision(_job(task, step_id, "key-rejected", "evt-rejected"))
    assert deferred.outcome == HandlerOutcome.RETRY_LATER

    _end_turn(db_session, turn)
    monkeypatch.setattr(
        "aiive.worker.handlers_goal_triggers.GoalDecisionEngine", _RecordingDecision,
    )
    completed = handle_goal_trigger_decision(_job(task, step_id, "key-rejected", "evt-rejected"))
    assert completed.outcome == HandlerOutcome.COMPLETED
    assert len(_RecordingDecision.calls) == 1


def test_expired_lease_with_running_status_recovers_after_restart(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """进程重启：Turn 行仍是 running 但租约已过期，必须能接手且只决策一次。"""
    thread = _thread(db_session)
    turn = _running_turn(db_session, thread)
    task = _confirmed_goal(db_session, thread)
    step_id = _arm_step(db_session, task, "key-lease")
    row = _enqueue(db_session, thread, key="evt-lease", index=0)
    row.status = "injected"
    row_id = row.id
    db_session.commit()

    db_session.query(TurnRecord).filter(TurnRecord.id == turn.id).update({
        TurnRecord.lease_expires_at: datetime.now(UTC) - timedelta(seconds=5),
    })
    db_session.commit()

    _RecordingDecision.calls = []
    monkeypatch.setattr(
        "aiive.worker.handlers_goal_triggers.GoalDecisionEngine", _RecordingDecision,
    )
    result = handle_goal_trigger_decision(_job(task, step_id, "key-lease", "evt-lease"))
    assert result.outcome == HandlerOutcome.COMPLETED
    assert len(_RecordingDecision.calls) == 1

    db_session.expire_all()
    stored = db_session.get(TurnEventInbox, row_id)
    assert stored is not None and stored.status == "expired"


# ---------------------------------------------------------------------------
# 3. handled / 缺失行
# ---------------------------------------------------------------------------


def test_handled_event_never_calls_model(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = _thread(db_session)
    _running_turn(db_session, thread)
    task = _confirmed_goal(db_session, thread)
    step_id = _arm_step(db_session, task, "key-handled")
    row = _enqueue(db_session, thread, key="evt-handled", index=0)
    row.status = "handled"
    row.decision = "notify"
    db_session.commit()

    monkeypatch.setattr(
        "aiive.worker.handlers_goal_triggers.GoalDecisionEngine", _FailingDecision,
    )
    result = handle_goal_trigger_decision(_job(task, step_id, "key-handled", "evt-handled"))
    assert result.outcome == HandlerOutcome.COMPLETED
    assert result.reason == "handled_in_turn"


def test_missing_inbox_row_still_defers_while_turn_active(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = _thread(db_session)
    _running_turn(db_session, thread)
    task = _confirmed_goal(db_session, thread)
    step_id = _arm_step(db_session, task, "key-missing")

    monkeypatch.setattr(
        "aiive.worker.handlers_goal_triggers.GoalDecisionEngine", _FailingDecision,
    )
    result = handle_goal_trigger_decision(
        _job(task, step_id, "key-missing", "evt-never-created"),
    )
    assert result.outcome == HandlerOutcome.RETRY_LATER
    assert result.reason == "foreground_turn_active"
