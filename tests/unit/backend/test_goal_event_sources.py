"""V53 通用计划事件源：真实运行事件 → 目标匹配 → 决策闭环（全离线，Fake LLM）。

覆盖：事件匹配、无匹配零 LLM 调用、任务成功/失败触发、窗口过期决策、重复事件幂等、
Outbox 失败重试、进程重启恢复、用户取消目标不被唤醒、主动消息交付，
以及知识维护与自进化修复的真实事件生产。

注意：Handler 会自行关闭会话，测试中一律用 id / 重新查询访问对象，避免读已分离实例。
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from aiive.db.models import AgentTask, Event, OutboxJob, Thread
from aiive.selfdev.repair_loop import persist_repair_run
from aiive.task_runtime.goal_events import (
    DISPATCH_JOB_TYPE,
    KNOWLEDGE_MAINTENANCE_COMPLETED,
    KNOWLEDGE_MAINTENANCE_FAILED,
    SELFDEV_REPAIR_COMPLETED,
    SELFDEV_REPAIR_FAILED,
    TASK_COMPLETED,
    event_from_job_payload,
    publish_goal_event,
)
from aiive.task_runtime.goal_service import GoalService
from aiive.task_runtime.goal_trigger_service import GoalTriggerService
from aiive.task_runtime.repository import TaskRepository, utcnow
from aiive.task_runtime.schemas import TaskStatus
from aiive.worker.handlers_goal_events import handle_goal_event_dispatch
from aiive.worker.handlers_goal_triggers import handle_goal_trigger_decision
from aiive.worker.handlers_knowledge import _publish as publish_knowledge_event
from aiive.worker.handlers_knowledge import handle_knowledge_maintenance
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome

DECISION_JOB_TYPE = "goal_trigger_decision"


class FakeLLM:
    """返回预设 JSON 的假模型；记录调用次数用于断言"零调用"。"""

    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload or {"action": "no_action", "reason": "nothing_to_do"}
        self.calls: list[list[dict]] = []

    def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        return SimpleNamespace(
            content=json.dumps(self.payload, ensure_ascii=False),
            model="fake-goal-model",
            latency_ms=1.0,
            usage={"prompt_tokens": 9, "completion_tokens": 5},
            trace_id=kwargs.get("trace_id"),
            finish_reason="stop",
        )


def _thread(db) -> str:
    thread_id = str(uuid.uuid4())
    db.add(Thread(id=thread_id, title="event source test"))
    db.flush()
    return thread_id


def _plan(steps: list[dict], **overrides) -> dict:
    plan = {
        "summary": "事件源测试",
        "timezone": "Asia/Shanghai",
        "acceptance": ["完成"],
        "steps": steps,
    }
    plan.update(overrides)
    return plan


def _confirmed(db, thread_id: str, plan: dict) -> str:
    task = TaskRepository(db).create_task(
        thread_id=thread_id,
        goal="事件源测试",
        title="事件源测试",
        task_type="long_term_goal",
        task_brief={"scope": {"allowed_capabilities": ["get_current_time"]}},
        enqueue=False,
    )
    task_id = task.id
    GoalService(db).attach_plan(task, plan)
    db.commit()
    GoalService(db).confirm(task_id)
    db.commit()
    return task_id


def _complete_task(db, thread_id: str, *, status: str = TaskStatus.SUCCEEDED.value) -> str:
    """真实走一遍任务终态：queued → dispatching → running → verifying → 终态。"""
    repo = TaskRepository(db)
    task = repo.create_task(
        thread_id=thread_id,
        goal="普通后台任务",
        title="普通后台任务",
        task_type="general",
        task_brief={"scope": {"allowed_capabilities": ["get_current_time"]}},
        enqueue=False,
    )
    task_id = task.id
    repo.transition_task(task, TaskStatus.DISPATCHING.value)
    repo.transition_task(task, TaskStatus.RUNNING.value)
    repo.transition_task(task, TaskStatus.VERIFYING.value)
    repo.transition_task(
        task, status, event_type="task_completed", event_payload={"status": status},
        visibility="conversation",
    )
    db.commit()
    return task_id


def _jobs(db, job_type: str) -> list[dict]:
    db.expire_all()
    return [
        {
            "id": row.id,
            "job_type": row.job_type,
            "payload": dict(row.payload or {}),
            "trace_id": row.trace_id,
            "max_retries": row.max_retries,
        }
        for row in db.query(OutboxJob).filter(OutboxJob.job_type == job_type).all()
    ]


def _dispatch_job(db) -> dict:
    jobs = _jobs(db, DISPATCH_JOB_TYPE)
    assert len(jobs) == 1, f"期望 1 条事件投递作业，实际 {len(jobs)}"
    return jobs[0]


def _claimed(job: dict, *, retry_count: int = 0) -> ClaimedJob:
    return ClaimedJob(
        id=job["id"], job_type=job["job_type"], payload=dict(job["payload"]),
        trace_id=job["trace_id"], retry_count=retry_count, max_retries=job["max_retries"],
        claim_token="claim-test", schema_version=1, worker_id="worker-test",
    )


def _patch_llm(monkeypatch, payload: dict | None = None) -> FakeLLM:
    fake = FakeLLM(payload)
    monkeypatch.setattr(
        "aiive.task_runtime.goal_decision.default_llm_client", lambda: fake,
    )
    return fake


def _plan_of(db, task_id: str):
    db.expire_all()
    stored = db.get(AgentTask, task_id)
    assert stored is not None
    return GoalService(db).load_plan(stored)


# ----------------------------------------------------------------------
# 1. 任务成功 → 事件投递 → 匹配 → 决策 → 主动消息
# ----------------------------------------------------------------------
def test_task_success_event_reaches_goal_and_delivers_message(db_session, monkeypatch) -> None:
    thread_id = _thread(db_session)
    task_id = _confirmed(db_session, thread_id, _plan([
        {"id": "followup", "title": "任务完成后跟进", "kind": "autonomous",
         "trigger": {
             "kind": "event_condition",
             "event_types": [TASK_COMPLETED],
             "match": {"status": "succeeded"},
         }},
    ]))
    _complete_task(db_session, thread_id, status=TaskStatus.SUCCEEDED.value)

    dispatch = _dispatch_job(db_session)
    assert dispatch["payload"]["event_type"] == TASK_COMPLETED
    assert dispatch["payload"]["thread_id"] == thread_id

    result = handle_goal_event_dispatch(_claimed(dispatch))
    assert result.outcome == HandlerOutcome.COMPLETED
    assert result.reason == "goal_event_matched:1"
    decisions = _jobs(db_session, DECISION_JOB_TYPE)
    assert len(decisions) == 1
    assert decisions[0]["payload"]["trigger_event_type"] == TASK_COMPLETED

    fake = _patch_llm(monkeypatch, {
        "action": "send_message",
        "message": "刚才的后台任务已完成，需要我继续下一步吗？",
        "reason": "task_succeeded",
    })
    outcome = handle_goal_trigger_decision(_claimed(decisions[0]))
    assert outcome.outcome == HandlerOutcome.COMPLETED
    assert len(fake.calls) == 1

    messages = db_session.query(Event).filter(
        Event.thread_id == thread_id,
        Event.event_type == "llm_response",
    ).all()
    assert len(messages) == 1
    assert messages[0].payload["proactive"] is True
    assert "后台任务已完成" in messages[0].payload["content"]
    assert _plan_of(db_session, task_id).step("followup").trigger_count == 1


# ----------------------------------------------------------------------
# 2. 事件类型/条件不匹配：投递照常，但零决策、零 LLM 调用
# ----------------------------------------------------------------------
def test_unmatched_event_produces_no_decision_and_no_llm(db_session, monkeypatch) -> None:
    thread_id = _thread(db_session)
    _confirmed(db_session, thread_id, _plan([
        {"id": "on_failure", "title": "只在失败时跟进", "kind": "autonomous",
         "trigger": {
             "kind": "event_condition",
             "event_types": [TASK_COMPLETED],
             "match": {"status": "failed"},
         }},
    ]))
    fake = _patch_llm(monkeypatch)
    _complete_task(db_session, thread_id, status=TaskStatus.SUCCEEDED.value)

    result = handle_goal_event_dispatch(_claimed(_dispatch_job(db_session)))
    assert result.reason == "goal_event_matched:0"
    assert _jobs(db_session, DECISION_JOB_TYPE) == []
    assert fake.calls == []


# ----------------------------------------------------------------------
# 2b. 任务失败同样触发：payload 的 status 参与条件匹配
# ----------------------------------------------------------------------
def test_task_failure_event_triggers_rule(db_session) -> None:
    thread_id = _thread(db_session)
    _confirmed(db_session, thread_id, _plan([
        {"id": "on_failure", "title": "失败后接手", "kind": "autonomous",
         "trigger": {
             "kind": "event_condition",
             "event_types": [TASK_COMPLETED],
             "match": {"status": "failed"},
         }},
    ]))
    _complete_task(db_session, thread_id, status=TaskStatus.FAILED.value)

    dispatch = _dispatch_job(db_session)
    assert dispatch["payload"]["event_payload"]["status"] == "failed"
    assert handle_goal_event_dispatch(_claimed(dispatch)).reason == "goal_event_matched:1"
    decisions = _jobs(db_session, DECISION_JOB_TYPE)
    assert len(decisions) == 1
    assert decisions[0]["payload"]["trigger_event_payload"]["status"] == "failed"


# ----------------------------------------------------------------------
# 3. 没有活跃目标订阅：事件在发布侧就被丢弃，不制造 Outbox 噪声
# ----------------------------------------------------------------------
def test_event_without_subscribers_is_dropped(db_session) -> None:
    thread_id = _thread(db_session)
    _complete_task(db_session, thread_id)
    assert _jobs(db_session, DISPATCH_JOB_TYPE) == []


# ----------------------------------------------------------------------
# 4. 重复事件 / 重复投递：只产生一次决策
# ----------------------------------------------------------------------
def test_duplicate_event_and_redelivery_are_idempotent(db_session) -> None:
    thread_id = _thread(db_session)
    _confirmed(db_session, thread_id, _plan([
        {"id": "wake", "title": "事件唤醒", "kind": "autonomous",
         "trigger": {"kind": "event_condition", "event_types": [TASK_COMPLETED]}},
    ]))
    assert publish_goal_event(
        db_session, event_type=TASK_COMPLETED, event_id="dup-1", source="task",
        thread_id=thread_id, payload={"task_id": "t1", "status": "succeeded"},
    ) is not None
    # 同一 source + event_id 的第二次发布被唯一键挡掉。
    assert publish_goal_event(
        db_session, event_type=TASK_COMPLETED, event_id="dup-1", source="task",
        thread_id=thread_id, payload={"task_id": "t1", "status": "succeeded"},
    ) is None
    db_session.commit()
    dispatch = _dispatch_job(db_session)

    assert handle_goal_event_dispatch(_claimed(dispatch)).reason == "goal_event_matched:1"
    # Outbox 重投同一作业：幂等键已写入计划，不产生第二条决策。
    assert handle_goal_event_dispatch(_claimed(dispatch)).reason == "goal_event_matched:0"
    assert len(_jobs(db_session, DECISION_JOB_TYPE)) == 1


# ----------------------------------------------------------------------
# 5. 消费失败沿用 Outbox 重试；恢复后仍只产生一条决策
# ----------------------------------------------------------------------
def test_dispatch_failure_is_retried_by_outbox(db_session, monkeypatch) -> None:
    thread_id = _thread(db_session)
    _confirmed(db_session, thread_id, _plan([
        {"id": "wake", "title": "事件唤醒", "kind": "autonomous",
         "trigger": {"kind": "event_condition", "event_types": [TASK_COMPLETED]}},
    ]))
    publish_goal_event(
        db_session, event_type=TASK_COMPLETED, event_id="retry-1", source="task",
        thread_id=thread_id, payload={"status": "succeeded"},
    )
    db_session.commit()
    dispatch = _dispatch_job(db_session)

    def _boom(self, event, **kwargs):
        raise RuntimeError("db_unavailable")

    monkeypatch.setattr(GoalTriggerService, "dispatch_event", _boom)
    failed = handle_goal_event_dispatch(_claimed(dispatch))
    assert failed.outcome == HandlerOutcome.RETRYABLE_ERROR
    assert _jobs(db_session, DECISION_JOB_TYPE) == []

    monkeypatch.undo()
    retried = handle_goal_event_dispatch(_claimed(dispatch, retry_count=1))
    assert retried.outcome == HandlerOutcome.COMPLETED
    assert retried.reason == "goal_event_matched:1"
    assert len(_jobs(db_session, DECISION_JOB_TYPE)) == 1


# ----------------------------------------------------------------------
# 6. 进程重启：待处理作业仍在 Outbox，重新消费不重复决策
# ----------------------------------------------------------------------
def test_pending_dispatch_survives_restart(db_session) -> None:
    thread_id = _thread(db_session)
    _confirmed(db_session, thread_id, _plan([
        {"id": "wake", "title": "事件唤醒", "kind": "autonomous",
         "trigger": {"kind": "event_condition", "event_types": [TASK_COMPLETED]}},
    ]))
    publish_goal_event(
        db_session, event_type=TASK_COMPLETED, event_id="restart-1", source="task",
        thread_id=thread_id, payload={"status": "succeeded"},
    )
    db_session.commit()
    dispatch = _dispatch_job(db_session)

    # 模拟进程重启：会话状态全部丢弃，只依赖已落库的作业与计划状态。
    db_session.expire_all()
    assert handle_goal_event_dispatch(_claimed(dispatch)).reason == "goal_event_matched:1"

    db_session.expire_all()
    replay = event_from_job_payload(dict(dispatch["payload"]))
    assert replay is not None
    assert GoalTriggerService(db_session).dispatch_event(replay) == 0
    assert len(_jobs(db_session, DECISION_JOB_TYPE)) == 1


# ----------------------------------------------------------------------
# 7. 用户取消/结束目标：不得被唤醒
# ----------------------------------------------------------------------
def test_cancelled_goal_is_not_woken(db_session, monkeypatch) -> None:
    thread_id = _thread(db_session)
    task_id = _confirmed(db_session, thread_id, _plan([
        {"id": "wake", "title": "事件唤醒", "kind": "autonomous",
         "trigger": {"kind": "event_condition", "event_types": [TASK_COMPLETED]}},
    ]))
    publish_goal_event(
        db_session, event_type=TASK_COMPLETED, event_id="cancel-1", source="task",
        thread_id=thread_id, payload={"status": "succeeded"},
    )
    db_session.commit()
    dispatch = _dispatch_job(db_session)

    GoalService(db_session).end(task_id, reason="用户结束目标")
    db_session.commit()

    fake = _patch_llm(monkeypatch)
    assert handle_goal_event_dispatch(_claimed(dispatch)).reason == "goal_event_matched:0"
    assert _jobs(db_session, DECISION_JOB_TYPE) == []
    assert fake.calls == []


# ----------------------------------------------------------------------
# 8. 窗口过期：触发一次决策并送达 LLM，重复扫描不重复
# ----------------------------------------------------------------------
def test_window_expiry_decision_is_delivered_once(db_session, monkeypatch) -> None:
    thread_id = _thread(db_session)
    now = utcnow()
    task_id = _confirmed(db_session, thread_id, _plan([
        {"id": "late", "title": "窗口任务", "kind": "autonomous",
         "trigger": {
             "kind": "window_once",
             "start": (now - timedelta(hours=2)).isoformat(),
             "end": (now - timedelta(hours=1)).isoformat(),
             "event_types": ["never_happens"],
         }},
    ]))
    assert GoalTriggerService(db_session).scan() == 1
    assert GoalTriggerService(db_session).scan() == 0

    decisions = _jobs(db_session, DECISION_JOB_TYPE)
    assert len(decisions) == 1
    assert decisions[0]["payload"]["reason"] == "window_expired"

    fake = _patch_llm(monkeypatch, {
        "action": "send_message",
        "message": "这个窗口已经错过了，要不要我调整时间？",
        "reason": "window_expired",
    })
    assert handle_goal_trigger_decision(_claimed(decisions[0])).outcome == HandlerOutcome.COMPLETED
    assert len(fake.calls) == 1
    messages = db_session.query(Event).filter(
        Event.thread_id == thread_id, Event.event_type == "llm_response",
    ).all()
    assert len(messages) == 1
    assert messages[0].payload["proactive"] is True
    assert _plan_of(db_session, task_id).step("late").expired_at is not None


# ----------------------------------------------------------------------
# 9. 知识维护完成：真实事件生产 + 全局匹配
# ----------------------------------------------------------------------
def test_knowledge_maintenance_publishes_completed_event(db_session) -> None:
    thread_id = _thread(db_session)
    _confirmed(db_session, thread_id, _plan([
        {"id": "after_cleanup", "title": "清理后复盘", "kind": "autonomous",
         "trigger": {"kind": "event_condition",
                     "event_types": [KNOWLEDGE_MAINTENANCE_COMPLETED]}},
    ]))
    job = OutboxJob(
        operation_id="knowledge_maintenance:test-1",
        job_type="knowledge_maintenance",
        status="running",
        payload={"operation_id": "test-1"},
        max_retries=3,
    )
    db_session.add(job)
    db_session.flush()
    claimed = _claimed({
        "id": job.id, "job_type": job.job_type, "payload": dict(job.payload),
        "trace_id": job.trace_id, "max_retries": job.max_retries,
    })

    result = handle_knowledge_maintenance(claimed)
    assert result.outcome == HandlerOutcome.COMPLETED

    dispatch = _dispatch_job(db_session)
    assert dispatch["payload"]["event_type"] == KNOWLEDGE_MAINTENANCE_COMPLETED
    assert dispatch["payload"]["thread_id"] is None
    # 全局事件能唤醒任意线程里的订阅目标。
    assert handle_goal_event_dispatch(_claimed(dispatch)).reason == "goal_event_matched:1"
    assert len(_jobs(db_session, DECISION_JOB_TYPE)) == 1


# ----------------------------------------------------------------------
# 10. 知识维护失败：只在最后一次尝试发布失败事件
# ----------------------------------------------------------------------
def test_knowledge_maintenance_failure_publishes_on_last_attempt(db_session, monkeypatch) -> None:
    thread_id = _thread(db_session)
    _confirmed(db_session, thread_id, _plan([
        {"id": "after_failure", "title": "维护失败后复盘", "kind": "autonomous",
         "trigger": {"kind": "event_condition",
                     "event_types": [KNOWLEDGE_MAINTENANCE_FAILED]}},
    ]))

    def _boom(db, *args, **kwargs):
        raise RuntimeError("scan_failed")

    published: list[dict] = []
    monkeypatch.setattr("aiive.worker.handlers_knowledge.scan_knowledge_base", _boom)
    monkeypatch.setattr(
        "aiive.worker.handlers_knowledge._publish",
        lambda db, operation_id, **kwargs: published.append(
            {"operation_id": operation_id, **kwargs}
        ),
    )
    job = OutboxJob(
        operation_id="knowledge_maintenance:test-2",
        job_type="knowledge_maintenance",
        status="running",
        payload={"operation_id": "test-2"},
        max_retries=3,
    )
    db_session.add(job)
    db_session.flush()
    claimed_job = {
        "id": job.id, "job_type": job.job_type, "payload": dict(job.payload),
        "trace_id": job.trace_id, "max_retries": job.max_retries,
    }

    # 非最后一次尝试：不发布失败事件（Outbox 还会重试）。
    first = handle_knowledge_maintenance(_claimed(claimed_job, retry_count=0))
    assert first.outcome == HandlerOutcome.RETRYABLE_ERROR
    assert published == []

    last = handle_knowledge_maintenance(_claimed(claimed_job, retry_count=2))
    assert last.outcome == HandlerOutcome.RETRYABLE_ERROR
    assert [item["status"] for item in published] == ["failed"]


def test_knowledge_event_publisher_writes_failed_dispatch_job(db_session) -> None:
    thread_id = _thread(db_session)
    _confirmed(db_session, thread_id, _plan([
        {"id": "after_failure", "title": "维护失败后复盘", "kind": "autonomous",
         "trigger": {"kind": "event_condition",
                     "event_types": [KNOWLEDGE_MAINTENANCE_FAILED]}},
    ]))
    publish_knowledge_event(
        db_session, "op-9", status="failed", totals={}, error="scan_failed",
    )
    db_session.commit()
    dispatch = _dispatch_job(db_session)
    assert dispatch["payload"]["event_type"] == KNOWLEDGE_MAINTENANCE_FAILED
    assert dispatch["payload"]["event_payload"]["status"] == "failed"


# ----------------------------------------------------------------------
# 11. 自进化修复结果：成功/失败各自成为真实事件
# ----------------------------------------------------------------------
def test_selfdev_repair_result_publishes_event(db_session) -> None:
    thread_id = _thread(db_session)
    _confirmed(db_session, thread_id, _plan([
        {"id": "review_repair", "title": "复盘修复结果", "kind": "autonomous",
         "trigger": {"kind": "event_condition", "event_types": [SELFDEV_REPAIR_COMPLETED]}},
    ]))

    persist_repair_run(db_session, {
        "run_id": "run-ok-1",
        "goal": "修复空指针",
        "status": "promoted",
        "ok": True,
        "final_operations": [],
    })
    jobs = _jobs(db_session, DISPATCH_JOB_TYPE)
    assert len(jobs) == 1
    assert jobs[0]["payload"]["event_type"] == SELFDEV_REPAIR_COMPLETED
    assert jobs[0]["payload"]["event_payload"]["run_id"] == "run-ok-1"

    persist_repair_run(db_session, {
        "run_id": "run-fail-1",
        "goal": "修复空指针",
        "status": "tests_failed",
        "ok": False,
        "failure_reason": "max_rounds_exhausted",
        "final_operations": [],
    })
    jobs = _jobs(db_session, DISPATCH_JOB_TYPE)
    assert len(jobs) == 2
    assert jobs[1]["payload"]["event_type"] == SELFDEV_REPAIR_FAILED


# ----------------------------------------------------------------------
# 12. 未知事件类型：拒绝发布，避免拼错的事件类型悄悄失效
# ----------------------------------------------------------------------
def test_unknown_event_type_is_rejected(db_session) -> None:
    thread_id = _thread(db_session)
    _confirmed(db_session, thread_id, _plan([
        {"id": "wake", "title": "事件唤醒", "kind": "autonomous",
         "trigger": {"kind": "event_condition", "event_types": [TASK_COMPLETED]}},
    ]))
    with pytest.raises(ValueError):
        publish_goal_event(
            db_session, event_type="not_a_real_event", event_id="x-1", source="task",
            thread_id=thread_id,
        )
