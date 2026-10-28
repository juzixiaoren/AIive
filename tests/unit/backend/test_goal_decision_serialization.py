"""V57 前后台 LLM 串行化与目标决策幂等验收测试（全离线，Fake LLM）。

覆盖：
1. 线程级占用互斥 + fencing + 租约过期恢复；
2. 后台决策让路前台占用（不并发调用模型）；
3. 前台 Turn 等待后台决策时用户输入不丢失，重试只续跑同一 Turn；
4. 决策持久化后崩溃 → 重投复用决策，不再调用模型；
5. 应用提交后 finalize 前崩溃 / 重复 Outbox 投递 → 只收尾，不重复副作用；
6. 主动消息 ID 由稳定决策标识派生（不依赖生成文案）；
7. 前台 Turn 正常收尾释放占用，后台可立即接手。

注意：生产代码会自行 open/close SessionLocal，而单元测试的分发器把它映射到
同一个测试会话，close 会让已加载的 ORM 实例 detach。因此本文件只用 id 字符串
传递身份，断言时重新查询。
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy.orm import Session

from aiive.core.llm_client import FakeLLMClient
from aiive.db.models import (
    AgentTask,
    AgentTaskEvent,
    Event,
    OutboxJob,
    Thread,
    TurnRecord,
)
from aiive.runtime import thread_lease
from aiive.runtime.proactive_message import ProactiveMessageService
from aiive.runtime.thread_lease import (
    OWNER_GOAL_DECISION,
    OWNER_TURN,
    acquire_thread_lease,
    lease_holder,
    release_thread_lease,
)
from aiive.runtime.turn_execution import TurnConflictError, TurnExecutionService
from aiive.task_runtime.goal_decision import GoalDecisionEngine, decision_id_for
from aiive.task_runtime.goal_plan import GoalStepStatus
from aiive.task_runtime.goal_service import GoalService
from aiive.task_runtime.goal_trigger_service import GoalTriggerService
from aiive.task_runtime.repository import TaskRepository, utcnow
from aiive.worker.handlers_goal_triggers import handle_goal_trigger_decision
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome
JOB_TYPE = "goal_trigger_decision"


class FakeLLM:
    """返回预设 JSON 的假模型；记录调用次数用于断言"不再调用模型"。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        return SimpleNamespace(
            content=json.dumps(self.payload, ensure_ascii=False),
            model="fake-goal-model",
            latency_ms=1.0,
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            trace_id=kwargs.get("trace_id"),
            finish_reason="stop",
        )


class _FakeChat:
    """支持 bind_tools/invoke 的假 ChatModel，返回无工具调用的简单回复。"""

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        return AIMessage(content="ok")


def _patch_llm(monkeypatch, payload: dict[str, Any]) -> FakeLLM:
    fake = FakeLLM(payload)
    monkeypatch.setattr(
        "aiive.task_runtime.goal_decision.default_llm_client", lambda: fake,
    )
    return fake


def _thread(db: Session) -> str:
    thread_id = str(uuid.uuid4())
    db.add(Thread(id=thread_id, title="v57"))
    db.commit()
    return thread_id


def _confirmed(db: Session, thread_id: str, steps: list[dict[str, Any]], **overrides: Any) -> str:
    plan = {
        "summary": "串行化测试",
        "timezone": "Asia/Shanghai",
        "acceptance": ["完成"],
        "steps": steps,
    }
    plan.update(overrides)
    task = TaskRepository(db).create_task(
        thread_id=thread_id,
        goal="串行化测试",
        title="串行化测试",
        task_type="long_term_goal",
        task_brief={"scope": {"allowed_capabilities": ["get_current_time"]}},
        enqueue=False,
    )
    GoalService(db).attach_plan(task, plan)
    GoalService(db).confirm(task.id)
    db.commit()
    return str(task.id)


def _due_step(step_id: str, title: str = "步骤") -> dict[str, Any]:
    return {
        "id": step_id,
        "title": title,
        "kind": "autonomous",
        "trigger": {"kind": "once_at", "at": (utcnow() - timedelta(minutes=1)).isoformat()},
    }


def _claimed(db: Session, task_id: str) -> ClaimedJob:
    row = db.query(OutboxJob).filter(
        OutboxJob.job_type == JOB_TYPE,
        OutboxJob.operation_id.like(f"goal_trigger:{task_id}:%"),
    ).one()
    return ClaimedJob(
        id=row.id, job_type=row.job_type, payload=dict(row.payload or {}),
        trace_id=row.trace_id, retry_count=0, max_retries=row.max_retries,
        claim_token="claim-v57", schema_version=1, worker_id="worker-v57",
    )


def _plan_of(db: Session, task_id: str):
    db.expire_all()
    task = db.get(AgentTask, task_id)
    assert task is not None
    plan = GoalService(db).load_plan(task)
    assert plan is not None
    return plan


def _messages(db: Session, thread_id: str) -> list[Event]:
    return db.query(Event).filter(
        Event.thread_id == thread_id, Event.event_type == "llm_response",
    ).all()


def _task_events(db: Session, task_id: str, event_type: str) -> list[AgentTaskEvent]:
    return db.query(AgentTaskEvent).filter(
        AgentTaskEvent.task_id == task_id,
        AgentTaskEvent.event_type == event_type,
    ).all()


# ----------------------------------------------------------------------
# 1. 线程级占用：互斥、fencing、只按 owner 释放
# ----------------------------------------------------------------------
def test_thread_lease_is_exclusive_and_fenced(db_session) -> None:
    thread_id = _thread(db_session)

    turn_lease = acquire_thread_lease(
        thread_id, owner_kind=OWNER_TURN, owner_id="turn-1", ttl_seconds=60,
    )
    assert turn_lease is not None
    assert turn_lease.fencing_token == 1

    # 后台决策抢占失败：同一线程同时只允许一个 LLM 持有者。
    assert acquire_thread_lease(
        thread_id, owner_kind=OWNER_GOAL_DECISION, owner_id="decision-1", ttl_seconds=60,
    ) is None

    holder = lease_holder(db_session, thread_id)
    assert holder is not None and holder["owner_kind"] == OWNER_TURN

    # 过期 token 不能续租，也不能删掉别人的占用。
    assert thread_lease.renew_thread_lease(
        thread_id, owner_kind=OWNER_TURN, owner_id="turn-1",
        fencing_token=99, ttl_seconds=60,
    ) is False
    assert release_thread_lease(
        db_session, thread_id, owner_kind=OWNER_TURN, owner_id="turn-1", fencing_token=99,
    ) is False
    assert lease_holder(db_session, thread_id) is not None

    assert thread_lease.renew_thread_lease(
        thread_id, owner_kind=OWNER_TURN, owner_id="turn-1",
        fencing_token=turn_lease.fencing_token, ttl_seconds=60,
    ) is True
    assert release_thread_lease(
        db_session, thread_id, owner_kind=OWNER_TURN, owner_id="turn-1",
        fencing_token=turn_lease.fencing_token,
    ) is True
    db_session.commit()
    assert lease_holder(db_session, thread_id) is None


# ----------------------------------------------------------------------
# 2. 崩溃后租约过期即可被接管（fencing 单调递增）
# ----------------------------------------------------------------------
def test_expired_lease_is_recovered_after_crash(db_session) -> None:
    thread_id = _thread(db_session)

    crashed = acquire_thread_lease(
        thread_id, owner_kind=OWNER_GOAL_DECISION, owner_id="decision-x", ttl_seconds=0,
    )
    assert crashed is not None
    # ttl=0 等价于持有者已崩溃：租约立即过期，其他持有者可直接接管。
    assert lease_holder(db_session, thread_id) is None

    recovered = acquire_thread_lease(
        thread_id, owner_kind=OWNER_TURN, owner_id="turn-2", ttl_seconds=60,
    )
    assert recovered is not None
    assert recovered.fencing_token == crashed.fencing_token + 1
    # 旧持有者用旧 token 续租必须失败（防止僵尸写入）。
    assert thread_lease.renew_thread_lease(
        thread_id, owner_kind=OWNER_GOAL_DECISION, owner_id="decision-x",
        fencing_token=crashed.fencing_token, ttl_seconds=60,
    ) is False


# ----------------------------------------------------------------------
# 3. 后台决策让路前台占用：不并发调用模型
# ----------------------------------------------------------------------
def test_background_decision_yields_to_foreground_lease(db_session, monkeypatch) -> None:
    thread_id = _thread(db_session)
    task_id = _confirmed(db_session, thread_id, [_due_step("report", "交周报")])
    assert GoalTriggerService(db_session).scan() == 1
    fake = _patch_llm(monkeypatch, {"action": "send_message", "message": "该交周报了"})

    # 前台 Turn 已取得线程占用（此处用占用行模拟"检查后 Turn 才启动"的竞态）。
    foreground = acquire_thread_lease(
        thread_id, owner_kind=OWNER_TURN, owner_id="turn-fg", ttl_seconds=60,
    )
    assert foreground is not None

    result = handle_goal_trigger_decision(_claimed(db_session, task_id))
    assert result.outcome == HandlerOutcome.RETRY_LATER
    assert result.reason == "thread_lease_busy"
    assert fake.calls == []
    assert _messages(db_session, thread_id) == []
    assert _task_events(db_session, task_id, "goal_trigger_decision_recorded") == []


# ----------------------------------------------------------------------
# 4. 前台 Turn 等待后台决策：用户输入不丢失，重试续跑同一 Turn
# ----------------------------------------------------------------------
def test_foreground_waits_for_background_decision_and_keeps_input(
    db_session, monkeypatch,
) -> None:
    thread_id = _thread(db_session)

    monkeypatch.setattr("aiive.runtime.turn_execution.FOREGROUND_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(thread_lease, "ACQUIRE_POLL_SECONDS", 0.02)
    monkeypatch.setattr(
        "aiive.runtime.turn_execution.ThreadBootstrapService.ensure_committed_thread",
        staticmethod(lambda tid=None: tid),
    )

    decision_id = decision_id_for("task-1", "step-1", "once_at:abc")
    goal_lease = acquire_thread_lease(
        thread_id, owner_kind=OWNER_GOAL_DECISION, owner_id=decision_id, ttl_seconds=60,
    )
    assert goal_lease is not None

    service = TurnExecutionService(llm_client=FakeLLMClient(), message_source="user")
    turn_id = str(uuid.uuid4())
    with pytest.raises(TurnConflictError) as blocked:
        service._resolve_and_preempt("帮我整理周报", thread_id, turn_id)  # pyright: ignore[reportPrivateUsage]
    assert str(blocked.value) == "turn_in_progress"

    # 用户输入已落库：Turn 停在 not_started，用户消息事件存在且只有一条。
    stored = db_session.query(TurnRecord).filter(
        TurnRecord.thread_id == thread_id, TurnRecord.turn_id == turn_id,
    ).one()
    assert stored.status == "not_started"
    user_events = db_session.query(Event).filter(
        Event.thread_id == thread_id, Event.event_type == "user_message",
    ).all()
    assert [item.payload["content"] for item in user_events] == ["帮我整理周报"]

    # 后台决策收尾交还占用后，同一 turn_id 重试直接续跑，不产生第二条用户消息。
    assert release_thread_lease(
        db_session, thread_id, owner_kind=OWNER_GOAL_DECISION,
        owner_id=decision_id, fencing_token=goal_lease.fencing_token,
    ) is True
    db_session.commit()

    turn, _execution_id, token = service._resolve_and_preempt(  # pyright: ignore[reportPrivateUsage]
        "帮我整理周报", thread_id, turn_id,
    )
    assert turn.status == "running"
    assert token >= 2
    assert db_session.query(Event).filter(
        Event.thread_id == thread_id, Event.event_type == "user_message",
    ).count() == 1
    holder = lease_holder(db_session, thread_id)
    assert holder is not None and holder["owner_kind"] == OWNER_TURN
    assert holder["owner_id"] == turn.id


# ----------------------------------------------------------------------
# 5. 决策持久化后崩溃：重投复用决策，不再调用模型
# ----------------------------------------------------------------------
def test_persisted_decision_is_reused_after_crash(db_session, monkeypatch) -> None:
    thread_id = _thread(db_session)
    task_id = _confirmed(db_session, thread_id, [_due_step("interview", "模拟面试")])
    assert GoalTriggerService(db_session).scan() == 1
    fake = _patch_llm(monkeypatch, {
        "action": "ask_user", "step_id": "interview",
        "message": "现在开始模拟面试吗？", "reason": "到期",
    })

    real_apply = GoalDecisionEngine.apply
    state = {"fail": True}

    def flaky_apply(self, **kwargs):
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("crash_after_decision_persisted")
        return real_apply(self, **kwargs)

    monkeypatch.setattr(GoalDecisionEngine, "apply", flaky_apply)

    first = handle_goal_trigger_decision(_claimed(db_session, task_id))
    assert first.outcome == HandlerOutcome.RETRYABLE_ERROR
    assert len(fake.calls) == 1
    assert len(_task_events(db_session, task_id, "goal_trigger_decision_recorded")) == 1
    assert _task_events(db_session, task_id, "goal_trigger_decision_applied") == []
    assert _messages(db_session, thread_id) == []

    second = handle_goal_trigger_decision(_claimed(db_session, task_id))
    assert second.outcome == HandlerOutcome.COMPLETED
    # 已持久化的决策直接复用：模型只被调用过一次。
    assert len(fake.calls) == 1
    assert len(_task_events(db_session, task_id, "goal_trigger_decision_recorded")) == 1
    assert len(_messages(db_session, thread_id)) == 1
    step = _plan_of(db_session, task_id).step("interview")
    assert step is not None
    assert step.status == GoalStepStatus.WAITING_USER.value


# ----------------------------------------------------------------------
# 6. 应用提交后 finalize 前崩溃 / 重复投递：只收尾，不重复副作用
# ----------------------------------------------------------------------
def test_repeated_delivery_only_finalizes(db_session, monkeypatch) -> None:
    thread_id = _thread(db_session)
    task_id = _confirmed(db_session, thread_id, [
        {"id": "a", "title": "A", "kind": "autonomous"},
        {"id": "b", "title": "B", "kind": "autonomous"},
        _due_step("c", "C"),
    ])
    assert GoalTriggerService(db_session).scan() == 1
    fake = _patch_llm(monkeypatch, {
        "action": "modify_plan",
        "plan_changes": {"summary": "新的目标摘要"},
        "reason": "调整",
    })
    claimed = _claimed(db_session, task_id)

    first = handle_goal_trigger_decision(claimed)
    assert first.outcome == HandlerOutcome.COMPLETED
    revision = _plan_of(db_session, task_id).revision
    assert revision == 3
    assert len(fake.calls) == 1

    # Outbox 在 finalize 前崩溃 → 同一作业被重投多次：只允许收尾。
    for _ in range(3):
        again = handle_goal_trigger_decision(claimed)
        assert again.outcome == HandlerOutcome.COMPLETED
        assert again.reason == "decision_already_applied"
    assert _plan_of(db_session, task_id).revision == revision
    assert len(fake.calls) == 1
    assert len(_task_events(db_session, task_id, "goal_trigger_decision_applied")) == 1
    assert len(_task_events(db_session, task_id, "goal_trigger_decision_recorded")) == 1
    assert len(_task_events(db_session, task_id, "goal_decision")) == 1


# ----------------------------------------------------------------------
# 7. 主动消息 ID 由稳定决策标识派生，不依赖生成文案
# ----------------------------------------------------------------------
def test_proactive_message_id_is_derived_from_decision(db_session, monkeypatch) -> None:
    thread_id = _thread(db_session)
    task_id = _confirmed(db_session, thread_id, [_due_step("push", "主动提醒")])
    assert GoalTriggerService(db_session).scan() == 1
    _patch_llm(monkeypatch, {"action": "send_message", "message": "该喝水了。", "reason": "到期"})

    claimed = _claimed(db_session, task_id)
    assert handle_goal_trigger_decision(claimed).outcome == HandlerOutcome.COMPLETED

    messages = _messages(db_session, thread_id)
    assert len(messages) == 1
    decision_id = decision_id_for(task_id, "push", claimed.payload["trigger_key"])
    expected = str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"proactive:{thread_id}:goal_decision:{decision_id}",
    ))
    assert messages[0].id == expected

    # 文案不同也不会生成第二条消息：ID 只由决策标识决定。
    posted = ProactiveMessageService(db_session).post(
        thread_id=thread_id,
        content="换一句完全不同的话",
        trace_id="trace-v57",
        dedupe_key=f"goal_decision:{decision_id}",
    )
    assert posted["created"] is False
    assert posted["event_id"] == expected
    assert len(_messages(db_session, thread_id)) == 1


# ----------------------------------------------------------------------
# 8. 前台 Turn 正常收尾释放占用，后台决策可立即接手
# ----------------------------------------------------------------------
def test_foreground_turn_releases_lease_after_completion(db_session, monkeypatch) -> None:
    thread_id = _thread(db_session)

    monkeypatch.setattr(
        "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
        lambda self: _FakeChat(),
    )
    monkeypatch.setattr(
        "aiive.runtime.turn_execution.ThreadBootstrapService.ensure_committed_thread",
        staticmethod(lambda tid=None: tid),
    )

    service = TurnExecutionService(llm_client=FakeLLMClient(), message_source="user")
    result = service.execute_turn("你好", thread_id=thread_id)
    assert result.get("reply") == "ok"

    assert lease_holder(db_session, thread_id) is None
    takeover = acquire_thread_lease(
        thread_id, owner_kind=OWNER_GOAL_DECISION, owner_id="decision-after-turn",
        ttl_seconds=60,
    )
    assert takeover is not None
