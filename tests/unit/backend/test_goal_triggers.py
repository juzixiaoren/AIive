"""目标步骤触发规则与触发决策闭环的验收测试（全离线，Fake LLM）。

覆盖：once_at / recurring（含时区）/ window_once / event_condition / opportunistic、
幂等与重启恢复、LLM 七种决策、计划修改、用户沉默、主动消息插入、前台 Turn 占用、
聊天不泄露系统提示词而 Trace 保留完整上下文。
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest

from aiive.db.models import (
    AgentTask,
    AgentTaskEvent,
    AgentTaskWatch,
    ContextSnapshot,
    Event,
    LLMCall,
    OutboxJob,
    Thread,
    TurnRecord,
)
from aiive.runtime.thread_state import ThreadState
from aiive.task_runtime.goal_decision import (
    SYSTEM_PROMPT,
    GoalDecision,
    GoalDecisionEngine,
)
from aiive.task_runtime.goal_plan import (
    GoalStepStatus,
    TriggerRule,
    next_runnable_step,
    next_time_trigger,
    parse_datetime,
    trigger_of,
)
from aiive.task_runtime.goal_service import GoalService
from aiive.task_runtime.goal_trigger_service import GoalTriggerService
from aiive.task_runtime.repository import TaskRepository, utcnow
from aiive.task_runtime.schemas import TaskStatus
from aiive.worker.handlers_goal_triggers import handle_goal_trigger_decision
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome

JOB_TYPE = "goal_trigger_decision"


class FakeLLM:
    """返回预设 JSON 的假模型；记录收到的 messages 供 Trace 断言。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[list[dict]] = []

    def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        return SimpleNamespace(
            content=json.dumps(self.payload, ensure_ascii=False),
            model="fake-goal-model",
            latency_ms=1.5,
            usage={"prompt_tokens": 11, "completion_tokens": 7},
            trace_id=kwargs.get("trace_id"),
            finish_reason="stop",
        )


def _thread(db) -> Thread:
    thread = Thread(id=str(uuid.uuid4()), title="trigger test")
    db.add(thread)
    db.flush()
    return thread


def _plan(steps: list[dict], **overrides) -> dict:
    plan = {
        "summary": "触发规则测试",
        "timezone": "Asia/Shanghai",
        "acceptance": ["完成"],
        "steps": steps,
    }
    plan.update(overrides)
    return plan


def _propose(db, thread: Thread, plan: dict):
    task = TaskRepository(db).create_task(
        thread_id=thread.id,
        goal="触发规则测试",
        title="触发规则测试",
        task_type="long_term_goal",
        task_brief={"scope": {"allowed_capabilities": ["get_current_time"]}},
        enqueue=False,
    )
    GoalService(db).attach_plan(task, plan)
    db.commit()
    return task


def _confirmed(db, thread: Thread, plan: dict):
    task = _propose(db, thread, plan)
    GoalService(db).confirm(task.id)
    db.commit()
    return task


def _object_id(value) -> str:
    """支持传对象或 id：先取 id 再过期，避免 Handler 关闭会话后读到已失效属性。"""
    return value if isinstance(value, str) else value.id


def _fresh(db, value):
    task_id = _object_id(value)
    db.expire_all()
    stored = db.get(AgentTask, task_id)
    assert stored is not None
    return stored


def _plan_of(db, value):
    return GoalService(db).load_plan(_fresh(db, value))


def _jobs(db, task_id: str) -> list[OutboxJob]:
    return db.query(OutboxJob).filter(
        OutboxJob.job_type == JOB_TYPE,
        OutboxJob.operation_id.like(f"goal_trigger:{task_id}:%"),
    ).all()


def _claimed(job: OutboxJob) -> ClaimedJob:
    return ClaimedJob(
        id=job.id, job_type=job.job_type, payload=dict(job.payload or {}),
        trace_id=job.trace_id, retry_count=0, max_retries=job.max_retries,
        claim_token="claim-test", schema_version=1, worker_id="worker-test",
    )


def _event(db, thread_id: str, event_type: str, payload: dict | None = None) -> Event:
    event = Event(
        id=str(uuid.uuid4()),
        trace_id=str(uuid.uuid4()),
        thread_id=thread_id,
        event_type=event_type,
        payload=dict(payload or {}),
        created_at=utcnow(),
    )
    db.add(event)
    db.flush()
    return event


def _patch_llm(monkeypatch, payload: dict) -> FakeLLM:
    fake = FakeLLM(payload)
    monkeypatch.setattr(
        "aiive.task_runtime.goal_decision.default_llm_client", lambda: fake,
    )
    return fake


# ----------------------------------------------------------------------
# 1. once_at：只触发一次，重复扫描/重启不重复入队
# ----------------------------------------------------------------------
def test_once_at_fires_exactly_once_across_rescans_and_restart(db_session) -> None:
    thread = _thread(db_session)
    at = (utcnow() - timedelta(minutes=1)).isoformat()
    task = _confirmed(db_session, thread, _plan([
        {"id": "report", "title": "交周报", "kind": "autonomous",
         "trigger": {"kind": "once_at", "at": at}},
    ]))

    assert GoalTriggerService(db_session).scan() == 1
    assert GoalTriggerService(db_session).scan() == 0
    # 模拟进程重启：新服务实例重新扫描，状态从计划里恢复，不重复触发。
    db_session.expire_all()
    assert GoalTriggerService(db_session).scan() == 0

    jobs = _jobs(db_session, task.id)
    assert len(jobs) == 1
    plan = _plan_of(db_session, task)
    assert plan.step("report").trigger_count == 1
    assert plan.step("report").last_trigger_key.startswith("once_at:")
    fired = db_session.query(AgentTaskEvent).filter(
        AgentTaskEvent.task_id == task.id,
        AgentTaskEvent.event_type == "goal_trigger_fired",
    ).all()
    assert len(fired) == 1


# ----------------------------------------------------------------------
# 2. recurring：时区正确、只触发已到点的那一场
# ----------------------------------------------------------------------
def test_recurring_respects_timezone_and_fires_only_due_occurrence(db_session) -> None:
    thread = _thread(db_session)
    rule = TriggerRule(
        kind="recurring", frequency="weekly", weekday=2, time="09:00",
        timezone="Asia/Shanghai",
    )
    task = _confirmed(db_session, thread, _plan([
        {"id": "standup", "title": "周会", "kind": "autonomous",
         "trigger": rule.model_dump(mode="json")},
    ]))

    occurrence = next_time_trigger(rule, utcnow())
    assert occurrence is not None
    before = occurrence - timedelta(minutes=30)
    assert GoalTriggerService(db_session).scan(now=before) == 0
    assert GoalTriggerService(db_session).scan(now=occurrence + timedelta(minutes=1)) == 1

    plan = _plan_of(db_session, task)
    step = plan.step("standup")
    assert step.trigger_count == 1
    assert step.next_trigger_at is not None
    assert parse_datetime(step.next_trigger_at) == occurrence + timedelta(days=7)
    # 同一场不会因为再次扫描而重复触发。
    assert GoalTriggerService(db_session).scan(now=occurrence + timedelta(minutes=2)) == 0


# ----------------------------------------------------------------------
# 3. window_once：窗口内首次条件满足触发一次
# ----------------------------------------------------------------------
def test_window_once_fires_on_first_matching_event_only(db_session) -> None:
    thread = _thread(db_session)
    now = utcnow()
    task = _confirmed(db_session, thread, _plan([
        {"id": "ingest", "title": "入库后跟进", "kind": "autonomous",
         "trigger": {
             "kind": "window_once",
             "start": (now - timedelta(hours=1)).isoformat(),
             "end": (now + timedelta(hours=2)).isoformat(),
             "event_types": ["knowledge_ingested"],
             "match": {"doc_id": "d1"},
         }},
    ]))

    assert GoalTriggerService(db_session).scan() == 0
    _event(db_session, thread.id, "knowledge_ingested", {"doc_id": "d2"})
    assert GoalTriggerService(db_session).scan() == 0
    first = _event(db_session, thread.id, "knowledge_ingested", {"doc_id": "d1"})
    assert GoalTriggerService(db_session).scan() == 1
    _event(db_session, thread.id, "knowledge_ingested", {"doc_id": "d1"})
    assert GoalTriggerService(db_session).scan() == 0

    jobs = _jobs(db_session, task.id)
    assert len(jobs) == 1
    assert jobs[0].payload["trigger_event_id"] == first.id


# ----------------------------------------------------------------------
# 4. window_once：窗口结束仍未满足 -> 过期并触发一次决策（重复扫描不重复触发）
# ----------------------------------------------------------------------
def test_window_once_expires_without_condition(db_session) -> None:
    thread = _thread(db_session)
    now = utcnow()
    task = _confirmed(db_session, thread, _plan([
        {"id": "late", "title": "窗口任务", "kind": "autonomous",
         "trigger": {
             "kind": "window_once",
             "start": (now - timedelta(hours=2)).isoformat(),
             "end": (now - timedelta(hours=1)).isoformat(),
             "event_types": ["never_happens"],
         }},
    ]))

    # V53：窗口到期本身是值得决策一次的真实事件（告知用户/调整计划/结束步骤）。
    assert GoalTriggerService(db_session).scan() == 1
    plan = _plan_of(db_session, task)
    step = plan.step("late")
    assert step.status == GoalStepStatus.MISSED.value
    assert step.expired_at is not None
    assert step.last_trigger_key.startswith("window_once_expired:")
    jobs = _jobs(db_session, task.id)
    assert len(jobs) == 1
    assert jobs[0].payload["reason"] == "window_expired"
    # 重复扫描不得重复触发。
    assert GoalTriggerService(db_session).scan() == 0
    assert len(_jobs(db_session, task.id)) == 1


# ----------------------------------------------------------------------
# 5. event_condition：类型 + payload 子集匹配
# ----------------------------------------------------------------------
def test_event_condition_matches_type_and_payload(db_session) -> None:
    thread = _thread(db_session)
    task = _confirmed(db_session, thread, _plan([
        {"id": "followup", "title": "任务完成后跟进", "kind": "autonomous",
         "trigger": {
             "kind": "event_condition",
             "event_types": ["task_completed"],
             "match": {"status": "succeeded"},
         }},
    ]))

    _event(db_session, thread.id, "task_completed", {"status": "failed"})
    assert GoalTriggerService(db_session).scan() == 0
    _event(db_session, thread.id, "task_completed", {"status": "succeeded"})
    assert GoalTriggerService(db_session).scan() == 1
    _event(db_session, thread.id, "task_completed", {"status": "succeeded"})
    assert GoalTriggerService(db_session).scan() == 0
    assert len(_jobs(db_session, task.id)) == 1


# ----------------------------------------------------------------------
# 6. opportunistic：冷却 + 每日上限，且没有事件时不触发
# ----------------------------------------------------------------------
def test_opportunistic_respects_cooldown_and_daily_cap(db_session) -> None:
    thread = _thread(db_session)
    now = utcnow()
    task = _confirmed(db_session, thread, _plan([
        {"id": "nudge", "title": "不定期跟进", "kind": "autonomous",
         "trigger": {
             "kind": "opportunistic",
             "event_types": ["task_completed"],
             "cooldown_hours": 2,
             "max_per_day": 1,
             "max_per_week": 5,
         }},
    ]))

    # 没有任何相关事件：即使扫描很多次也不触发，更不调用模型。
    assert GoalTriggerService(db_session).scan(now=now) == 0
    assert GoalTriggerService(db_session).scan(now=now + timedelta(hours=1)) == 0

    _event(db_session, thread.id, "task_completed", {"status": "succeeded"})
    assert GoalTriggerService(db_session).scan(now=now + timedelta(minutes=1)) == 1
    plan = _plan_of(db_session, task)
    assert plan.step("nudge").cooldown_until is not None

    _event(db_session, thread.id, "task_completed", {"status": "succeeded"})
    assert GoalTriggerService(db_session).scan(now=now + timedelta(minutes=30)) == 0
    # 冷却结束后仍受"每天 1 次"上限约束。
    assert GoalTriggerService(db_session).scan(now=now + timedelta(hours=3)) == 0
    assert len(_jobs(db_session, task.id)) == 1


# ----------------------------------------------------------------------
# 7. 任务执行中不并发触发；执行结束后不丢事件
# ----------------------------------------------------------------------
def test_scan_skips_running_task_without_losing_event(db_session) -> None:
    thread = _thread(db_session)
    task = _confirmed(db_session, thread, _plan([
        {"id": "wake", "title": "事件唤醒", "kind": "autonomous",
         "trigger": {"kind": "event_condition", "event_types": ["task_completed"]}},
    ]))
    _event(db_session, thread.id, "task_completed", {"status": "succeeded"})

    stored = _fresh(db_session, task)
    stored.status = TaskStatus.RUNNING.value
    db_session.flush()
    assert GoalTriggerService(db_session).scan() == 0

    stored.status = TaskStatus.BLOCKED_USER.value
    db_session.flush()
    assert GoalTriggerService(db_session).scan() == 1


# ----------------------------------------------------------------------
# 8. Handler：no_action 不改计划，但留下决策审计与完整 Trace
# ----------------------------------------------------------------------
def test_handler_no_action_keeps_plan_and_writes_trace(db_session, monkeypatch) -> None:
    thread = _thread(db_session)
    thread_id = _object_id(thread)
    task = _confirmed(db_session, thread, _plan([
        {"id": "later", "title": "稍后处理", "kind": "autonomous",
         "trigger": {"kind": "once_at", "at": (utcnow() - timedelta(minutes=1)).isoformat()}},
    ]))
    GoalTriggerService(db_session).scan()
    task_id = task.id
    job = _jobs(db_session, task_id)[0]
    claimed = _claimed(job)
    trace_id = str(job.trace_id)
    fake = _patch_llm(monkeypatch, {"action": "no_action", "reason": "时机不合适"})

    result = handle_goal_trigger_decision(claimed)
    assert result.outcome == HandlerOutcome.COMPLETED
    assert len(fake.calls) == 1

    plan = _plan_of(db_session, task_id)
    assert plan.step("later").status == GoalStepStatus.PENDING.value
    # confirm 时 revision 由 1 递增到 2；no_action 不得再改动计划版本。
    assert plan.revision == 2
    decisions = db_session.query(AgentTaskEvent).filter(
        AgentTaskEvent.task_id == task_id,
        AgentTaskEvent.event_type == "goal_decision",
    ).all()
    assert len(decisions) == 1

    snapshot = db_session.query(ContextSnapshot).filter(ContextSnapshot.trace_id == trace_id).one()
    assert snapshot.context_items[0]["role"] == "system"
    assert SYSTEM_PROMPT[:20] in snapshot.context_items[0]["content"]
    call = db_session.query(LLMCall).filter(LLMCall.trace_id == trace_id).one()
    assert "长期目标守护进程" in (call.input_preview or "")
    # 聊天时间线没有任何本轮内容：no_action 不打扰用户。
    assert db_session.query(Event).filter(
        Event.thread_id == thread_id, Event.event_type == "llm_response",
    ).count() == 0


# ----------------------------------------------------------------------
# 9. Handler：run_step 只入队一次，服务端复核依赖
# ----------------------------------------------------------------------
def test_handler_run_step_enqueues_single_agent_run(db_session, monkeypatch) -> None:
    thread = _thread(db_session)
    task = _confirmed(db_session, thread, _plan([
        {"id": "write", "title": "写总结", "kind": "autonomous",
         "trigger": {"kind": "once_at", "at": (utcnow() - timedelta(minutes=1)).isoformat()}},
    ]))
    GoalTriggerService(db_session).scan()
    task_id = task.id
    claimed = _claimed(_jobs(db_session, task_id)[0])
    _patch_llm(monkeypatch, {"action": "run_step", "step_id": "write", "reason": "到期"})

    result = handle_goal_trigger_decision(claimed)
    assert result.outcome == HandlerOutcome.COMPLETED

    plan = _plan_of(db_session, task_id)
    assert plan.step("write").status == GoalStepStatus.READY.value
    assert _fresh(db_session, task_id).status == TaskStatus.QUEUED.value
    runs = db_session.query(OutboxJob).filter(
        OutboxJob.job_type == "agent_task_run",
        OutboxJob.operation_id.like(f"agent_task_run:{task_id}:%"),
    ).all()
    assert len(runs) == 1


# ----------------------------------------------------------------------
# 10. Handler：modify_plan 走 GoalService.modify 的字段级校验
# ----------------------------------------------------------------------
def test_engine_modify_plan_applies_revision(db_session) -> None:
    thread = _thread(db_session)
    task = _confirmed(db_session, thread, _plan([
        {"id": "a", "title": "A", "kind": "autonomous"},
        {"id": "b", "title": "B", "kind": "autonomous"},
    ]))
    engine = GoalDecisionEngine(db_session)
    decision = GoalDecision(
        action="modify_plan",
        plan_changes={"steps": [
            {"id": "a", "title": "A2", "kind": "autonomous"},
            {"id": "b", "title": "B", "kind": "autonomous"},
        ]},
        reason="调整",
    )
    outcome = engine.apply(
        task_id=_object_id(task), step_id="a", trigger_key="", decision=decision,
    )
    assert outcome["status"] == "applied"
    plan = _plan_of(db_session, task)
    assert plan.revision == 3
    assert plan.step("a").title == "A2"


# ----------------------------------------------------------------------
# 11. Handler：complete_goal 必须通过证据复核
# ----------------------------------------------------------------------
def test_complete_goal_requires_evidence(db_session) -> None:
    thread = _thread(db_session)
    task = _confirmed(db_session, thread, _plan([
        {"id": "only", "title": "唯一步骤", "kind": "autonomous"},
    ]))
    goals = GoalService(db_session)
    plan = goals.load_plan(_fresh(db_session, task))
    plan.step("only").status = GoalStepStatus.DONE
    plan.step("only").evidence_refs = []
    goals.save_plan(_fresh(db_session, task), plan)
    db_session.commit()

    with pytest.raises(Exception) as missing:
        goals.complete_goal(_object_id(task))
    assert "goal_missing_evidence" in str(missing.value)

    plan = goals.load_plan(_fresh(db_session, task))
    plan.step("only").evidence_refs = ["ev-1"]
    goals.save_plan(_fresh(db_session, task), plan)
    db_session.commit()

    detail = goals.complete_goal(_object_id(task))
    assert detail["plan"]["status"] == "completed"
    assert _fresh(db_session, task).status == TaskStatus.SUCCEEDED.value


# ----------------------------------------------------------------------
# 12. 主动消息：真实助手消息进入聊天时间线，而不是通知
# ----------------------------------------------------------------------
def test_ask_user_posts_real_assistant_message(db_session, monkeypatch) -> None:
    thread = _thread(db_session)
    thread_id = _object_id(thread)
    task = _confirmed(db_session, thread, _plan([
        {"id": "interview", "title": "模拟面试", "kind": "autonomous",
         "trigger": {"kind": "once_at", "at": (utcnow() - timedelta(minutes=1)).isoformat()}},
    ]))
    GoalTriggerService(db_session).scan()
    task_id = task.id
    claimed = _claimed(_jobs(db_session, task_id)[0])
    _patch_llm(monkeypatch, {
        "action": "ask_user", "step_id": "interview",
        "message": "今天的模拟面试到点了，现在开始吗？", "reason": "到期",
    })

    result = handle_goal_trigger_decision(claimed)
    assert result.outcome == HandlerOutcome.COMPLETED

    page = ThreadState(db_session).list_thread_messages_page(thread_id=thread_id)
    contents = [item["content"] for item in page["messages"]]
    assert "今天的模拟面试到点了，现在开始吗？" in contents
    turn = db_session.query(TurnRecord).filter(TurnRecord.thread_id == thread_id).one()
    assert turn.source == "runtime_event"
    assert turn.status == "completed"
    # 不是后台通知：没有 notification_created 事件。
    assert db_session.query(Event).filter(
        Event.thread_id == thread_id, Event.event_type == "notification_created",
    ).count() == 0
    assert _plan_of(db_session, task_id).step("interview").status == GoalStepStatus.WAITING_USER.value


# ----------------------------------------------------------------------
# 13. 聊天不泄露系统提示词，Trace 保留完整输入
# ----------------------------------------------------------------------
def test_chat_hides_system_prompt_but_trace_keeps_it(db_session, monkeypatch) -> None:
    thread = _thread(db_session)
    thread_id = _object_id(thread)
    task = _confirmed(db_session, thread, _plan([
        {"id": "msg", "title": "发消息", "kind": "autonomous",
         "trigger": {"kind": "once_at", "at": (utcnow() - timedelta(minutes=1)).isoformat()}},
    ]))
    GoalTriggerService(db_session).scan()
    job = _jobs(db_session, task.id)[0]
    claimed = _claimed(job)
    trace_id = str(job.trace_id)
    _patch_llm(monkeypatch, {
        "action": "send_message", "message": "提醒你下午三点有复盘。", "reason": "到期",
    })
    assert handle_goal_trigger_decision(claimed).outcome == HandlerOutcome.COMPLETED

    page = ThreadState(db_session).list_thread_messages_page(thread_id=thread_id)
    chat_text = "\n".join(item["content"] for item in page["messages"])
    assert "提醒你下午三点有复盘。" in chat_text
    assert "长期目标守护进程" not in chat_text
    assert "goal_trigger_decision" not in chat_text

    snapshot = db_session.query(ContextSnapshot).filter(ContextSnapshot.trace_id == trace_id).one()
    trace_text = json.dumps(snapshot.context_items, ensure_ascii=False)
    assert "长期目标守护进程" in trace_text
    assert "trigger" in trace_text
    decision_event = db_session.query(Event).filter(
        Event.trace_id == trace_id, Event.event_type == "goal_trigger_decision",
    ).one()
    assert decision_event.payload["messages"][0]["role"] == "system"


# ----------------------------------------------------------------------
# 14. 前台 Turn 正在执行：主动消息延后，不并发写入；结束后按序补发
# ----------------------------------------------------------------------
def test_proactive_message_deferred_while_foreground_turn_runs(db_session, monkeypatch) -> None:
    thread = _thread(db_session)
    thread_id = _object_id(thread)
    foreground = TurnRecord(
        thread_id=thread_id, turn_id=str(uuid.uuid4()), turn_sequence=1,
        status="running", source="user", request_fingerprint="fg",
        lease_expires_at=utcnow() + timedelta(minutes=5),
    )
    db_session.add(foreground)
    db_session.commit()
    foreground_id = foreground.id

    task = _confirmed(db_session, thread, _plan([
        {"id": "push", "title": "主动提醒", "kind": "autonomous",
         "trigger": {"kind": "once_at", "at": (utcnow() - timedelta(minutes=1)).isoformat()}},
    ]))
    GoalTriggerService(db_session).scan()
    task_id = task.id
    claimed = _claimed(_jobs(db_session, task_id)[0])
    _patch_llm(monkeypatch, {"action": "send_message", "message": "该喝水了。", "reason": "到期"})

    first = handle_goal_trigger_decision(claimed)
    assert first.outcome == HandlerOutcome.RETRY_LATER
    assert db_session.query(Event).filter(
        Event.thread_id == thread_id, Event.event_type == "llm_response",
    ).count() == 0

    stored = db_session.get(TurnRecord, foreground_id)
    assert stored is not None
    stored.status = "completed"
    stored.lease_expires_at = None
    db_session.commit()

    second = handle_goal_trigger_decision(claimed)
    assert second.outcome == HandlerOutcome.COMPLETED
    replies = db_session.query(Event).filter(
        Event.thread_id == thread_id, Event.event_type == "llm_response",
    ).all()
    assert len(replies) == 1
    proactive = db_session.query(TurnRecord).filter(
        TurnRecord.thread_id == thread_id, TurnRecord.source == "runtime_event",
    ).one()
    assert proactive.turn_sequence == 2
    # 重试不会重复插入同一条消息。
    assert handle_goal_trigger_decision(claimed).outcome == HandlerOutcome.COMPLETED
    assert db_session.query(Event).filter(
        Event.thread_id == thread_id, Event.event_type == "llm_response",
    ).count() == 1


# ----------------------------------------------------------------------
# 15. 用户沉默只阻塞依赖它的步骤，独立步骤照常触发
# ----------------------------------------------------------------------
def test_user_silence_blocks_only_dependent_steps(db_session) -> None:
    thread = _thread(db_session)
    task = _confirmed(db_session, thread, _plan([
        {"id": "ask", "title": "等用户回答", "kind": "autonomous"},
        {"id": "independent", "title": "独立步骤", "kind": "autonomous",
         "trigger": {"kind": "once_at", "at": (utcnow() - timedelta(minutes=1)).isoformat()}},
        {"id": "dependent", "title": "依赖回答", "kind": "autonomous",
         "depends_on": ["ask"],
         "trigger": {"kind": "once_at", "at": (utcnow() - timedelta(minutes=1)).isoformat()}},
    ]))
    GoalService(db_session).mark_step_waiting_user(_object_id(task), "ask", "请确认时间")
    db_session.commit()

    assert GoalTriggerService(db_session).scan() == 1
    jobs = _jobs(db_session, _object_id(task))
    assert len(jobs) == 1
    assert jobs[0].payload["step_id"] == "independent"


# ----------------------------------------------------------------------
# 16. 旧 schedule 写法继续可用（向后兼容）
# ----------------------------------------------------------------------
def test_legacy_schedule_is_normalized_to_trigger(db_session) -> None:
    thread = _thread(db_session)
    at = (utcnow() - timedelta(minutes=1)).isoformat()
    task = _confirmed(db_session, thread, _plan([
        {"id": "legacy", "title": "旧写法", "kind": "autonomous",
         "schedule": {"kind": "once", "at": at}},
    ]))
    plan = _plan_of(db_session, task)
    rule = trigger_of(plan.step("legacy"))
    assert rule is not None and rule.kind.value == "once_at"
    assert GoalTriggerService(db_session).scan() == 1

# ----------------------------------------------------------------------
# 17. window_once：同一窗口内只触发一次（第二个匹配事件不再重复入队）
# ----------------------------------------------------------------------
def test_window_once_fires_only_once_per_window(db_session) -> None:
    thread = _thread(db_session)
    start = (utcnow() - timedelta(minutes=5)).isoformat()
    end = (utcnow() + timedelta(hours=1)).isoformat()
    task = _confirmed(db_session, thread, _plan([
        {"id": "watch", "title": "窗口观察", "kind": "autonomous",
         "trigger": {"kind": "window_once", "start": start, "end": end,
                     "event_types": ["user_message"]}},
    ]))
    _event(db_session, _object_id(thread), "user_message", {"content": "第一条"})
    assert GoalTriggerService(db_session).scan() == 1

    # 窗口仍未结束，又来了一个匹配事件：不得二次触发。
    _event(db_session, _object_id(thread), "user_message", {"content": "第二条"})
    assert GoalTriggerService(db_session).scan() == 0
    assert len(_jobs(db_session, _object_id(task))) == 1


# ----------------------------------------------------------------------
# 18. 显式触发规则不会被旧的时间排期路径自动起跑，也不建 time watch
# ----------------------------------------------------------------------
def test_explicit_trigger_step_never_auto_starts(db_session) -> None:
    thread = _thread(db_session)
    start = (utcnow() - timedelta(minutes=5)).isoformat()
    end = (utcnow() + timedelta(hours=1)).isoformat()
    task = _confirmed(db_session, thread, _plan([
        {"id": "watch", "title": "窗口观察", "kind": "autonomous",
         "trigger": {"kind": "window_once", "start": start, "end": end}},
    ]))
    assert GoalTriggerService(db_session).scan() == 1

    plan = _plan_of(db_session, _object_id(task))
    # 窗口起点已经过去，但步骤仍应由规则命中驱动，不能按时间自动起跑。
    assert next_runnable_step(plan, utcnow()) is None
    assert db_session.query(AgentTaskWatch).count() == 0


# ----------------------------------------------------------------------
# 19. 修改计划不得重置触发状态，否则同一规则会二次触发并重复发消息
# ----------------------------------------------------------------------
def test_modify_keeps_trigger_state(db_session) -> None:
    thread = _thread(db_session)
    at = (utcnow() - timedelta(minutes=1)).isoformat()
    task = _confirmed(db_session, thread, _plan([
        {"id": "report", "title": "交周报", "kind": "autonomous",
         "trigger": {"kind": "once_at", "at": at}},
    ]))
    assert GoalTriggerService(db_session).scan() == 1
    key = _plan_of(db_session, _object_id(task)).step("report").last_trigger_key
    assert key

    GoalService(db_session).modify(_object_id(task), {"summary": "新的目标摘要"})
    db_session.commit()

    plan = _plan_of(db_session, _object_id(task))
    assert plan.step("report").last_trigger_key == key
    assert GoalTriggerService(db_session).scan() == 0
