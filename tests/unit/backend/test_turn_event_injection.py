"""安全边界上的主动事件注入与协作式处理（Fake LLM，全离线）。

覆盖：工具执行期间事件到达、连续工具调用、多事件合并/排序、注入上限、
主动消息写入唯一时间线与顺序、原任务恢复、重复事件、Outbox 兜底与重启恢复、
Trace 完整性，以及内部提示词不进入普通聊天界面。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from aiive.context.run_context import RunContext
from aiive.core.llm_client import FakeLLMClient
from aiive.db.models import (
    AgentTask,
    Event,
    Thread,
    TurnEventInbox,
    TurnRecord,
)
from aiive.runtime.agent_graph import AgentGraph, AgentGraphResult, _tool_calls_paired
from aiive.runtime.proactive_tool import NOTIFY_USER_TOOL_ID, _notify_user, register_notify_user
from aiive.runtime.thread_state import ThreadState
from aiive.runtime.turn_events import (
    INTERNAL_CONTEXT_HEADER,
    enqueue_turn_event,
)
from aiive.runtime.turn_execution import TurnExecutionService, TurnHeartbeat
from aiive.task_runtime.goal_service import GoalService
from aiive.task_runtime.repository import TaskRepository
from aiive.tools.langchain_adapter import build_langchain_tools
from aiive.tools.registry import CapabilitySafetySchema, ToolRegistration, ToolRegistry
from aiive.worker.handlers_goal_triggers import handle_goal_trigger_decision
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


def _thread(db) -> Thread:
    thread = Thread(id=str(uuid.uuid4()), title="turn event injection")
    db.add(thread)
    db.flush()
    return thread


def _running_turn(db, thread: Thread) -> TurnRecord:
    now = datetime.now(timezone.utc)
    turn = TurnRecord(
        thread_id=thread.id,
        turn_id=str(uuid.uuid4()),
        turn_sequence=1,
        status="running",
        execution_id="exec-1",
        lease_expires_at=now + timedelta(minutes=5),
        request_fingerprint="fp",
        source="user",
    )
    db.add(turn)
    db.commit()
    return turn


def _registry(thread_id: str, turn: TurnRecord, on_echo=None):
    def echo(text: str, ctx: RunContext | None = None) -> dict:
        if on_echo is not None:
            on_echo()
        return {"ok": True, "echo": text}

    registry = ToolRegistry()
    registry.register(ToolRegistration(
        safety=CapabilitySafetySchema(
            capability_id="echo",
            definition_source="test",
            definition_trust_level="trusted_core",
            risk_level="low",
            effect_mode="db_transactional",
        ),
        handler=echo,
        description="原任务使用的工具",
        parameters={"text": {"type": "str", "required": True}},
    ))
    register_notify_user(registry)
    tools = build_langchain_tools(
        registry,
        run_context=RunContext(
            thread_id=thread_id,
            trace_id="trace-inject",
            turn_id=turn.turn_id,
            turn_record_id=turn.id,
        ),
    )
    return registry, tools


class RecordingAssistant:
    """按顺序返回预设 AIMessage，并记录每次真实收到的消息。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)
        self.calls: list[list] = []

    def invoke(self, messages):
        self.calls.append(list(messages))
        index = len(self.calls) - 1
        if index < len(self.responses):
            return self.responses[index]
        return AIMessage(content="done")


def _run_graph(db, thread: Thread, turn: TurnRecord, assistant, registry, tools):
    graph = AgentGraph(FakeLLMClient(), db)
    compiled, _, _ = graph._build_graph(
        assistant, tools, registry,
        trace_id="trace-inject",
        thread_id=thread.id,
        turn_record_id=turn.id,
        execution_id=turn.execution_id or "exec-1",
        turn_id=turn.turn_id,
    )
    return compiled.invoke({
        "messages": [HumanMessage(content="原任务：整理资料")],
        "tool_records": [],
    })


def _enqueue(db, thread: Thread, *, key: str, priority: int = 50, label: str = "") -> TurnEventInbox:
    row = enqueue_turn_event(
        db,
        thread_id=thread.id,
        event_type="goal_trigger_fired",
        source="goal_trigger",
        dedupe_key=key,
        priority=priority,
        payload={"reason": label or key, "task_id": "", "step_id": "s1"},
    )
    db.commit()
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# 1. 工具执行期间到达 → 批次完成后注入；不增加额外 LLM 调用
# ---------------------------------------------------------------------------


def test_event_arriving_during_tool_execution_is_injected_after_batch(db_session) -> None:
    thread = _thread(db_session)
    turn = _running_turn(db_session, thread)
    thread_id = thread.id
    injected_ids: list[str] = []

    def on_echo() -> None:
        # 工具正在执行时事件到达：持久化到收件箱，等待安全边界。
        from aiive.db.base import SessionLocal

        tool_db = SessionLocal()
        try:
            row = enqueue_turn_event(
                tool_db,
                thread_id=thread_id,
                event_type="goal_trigger_fired",
                source="goal_trigger",
                dedupe_key="goal_trigger:op-during-tool",
                priority=80,
                payload={"reason": "任务完成事件", "task_id": "", "step_id": "s1"},
            )
            tool_db.commit()
            assert row is not None
            injected_ids.append(row.id)
        finally:
            tool_db.close()

    registry, tools = _registry(thread_id, turn, on_echo=on_echo)
    assistant = RecordingAssistant([
        AIMessage(content="", tool_calls=[{
            "id": "call-echo-1", "name": "echo", "args": {"text": "batch-1"},
        }]),
        AIMessage(content="[[EVENT_DECISION: continue]]原任务完成"),
    ])
    result = _run_graph(db_session, thread, turn, assistant, registry, tools)

    # 第一次调用时收件箱为空；工具执行中事件才到达；第二次调用注入。
    assert len(assistant.calls) == 2, "事件注入不得额外发起 LLM 请求"
    first_system = [m for m in assistant.calls[0] if isinstance(m, SystemMessage)]
    assert not any(INTERNAL_CONTEXT_HEADER in str(m.content) for m in first_system)
    second_messages = assistant.calls[1]
    assert isinstance(second_messages[-1], SystemMessage)
    assert INTERNAL_CONTEXT_HEADER in str(second_messages[-1].content)
    assert "任务完成事件" in str(second_messages[-1].content)

    final = [m for m in result["messages"] if isinstance(m, AIMessage)][-1]
    assert str(final.content) == "原任务完成", "内部决策标记必须从最终答复剥离"
    assert result["event_decisions"][0]["decision"] == "continue"

    db_session.expire_all()
    stored = db_session.get(TurnEventInbox, injected_ids[0])
    assert stored is not None and stored.status == "injected"
    assert stored.decision == "continue"

    trace = db_session.query(Event).filter(Event.event_type == "turn_event_injection").one()
    assert INTERNAL_CONTEXT_HEADER in trace.payload["internal_context"]
    assert trace.payload["decision"] == "continue"
    assert trace.trace_id == "trace-inject"


# ---------------------------------------------------------------------------
# 2. 流式标记过滤与配对边界
# ---------------------------------------------------------------------------


def test_stream_filter_strips_internal_decision_marker() -> None:
    from aiive.runtime.agent_graph import _DecisionMarkerStreamFilter

    marker = _DecisionMarkerStreamFilter()
    streamed = "".join(
        marker.feed(part)
        for part in ["[[EVENT", "_DECISION: ", "continue]]", "最终", "答复"]
    ) + marker.flush()
    assert streamed == "最终答复"

    normal = _DecisionMarkerStreamFilter()
    streamed_normal = "".join(
        normal.feed(part) for part in ["普通", "回答"]
    ) + normal.flush()
    assert streamed_normal == "普通回答"




def test_no_injection_while_tool_call_unpaired() -> None:
    call = AIMessage(content="", tool_calls=[{
        "id": "call-1", "name": "echo", "args": {"text": "x"},
    }])
    assert _tool_calls_paired([call]) is False
    paired = [call, ToolMessage(content="ok", tool_call_id="call-1", name="echo")]
    assert _tool_calls_paired(paired) is True


# ---------------------------------------------------------------------------
# 3. 多事件合并、优先级排序与有界注入
# ---------------------------------------------------------------------------


def test_multiple_events_merge_in_priority_order(db_session) -> None:
    thread = _thread(db_session)
    turn = _running_turn(db_session, thread)
    low = _enqueue(db_session, thread, key="evt-low", priority=50, label="低优先级")
    high = _enqueue(db_session, thread, key="evt-high", priority=90, label="高优先级")
    mid = _enqueue(db_session, thread, key="evt-mid", priority=80, label="中优先级")

    registry, tools = _registry(thread.id, turn)
    assistant = RecordingAssistant([AIMessage(content="[[EVENT_DECISION: no_action]]完成")])
    _run_graph(db_session, thread, turn, assistant, registry, tools)

    assert len(assistant.calls) == 1
    internal = str(assistant.calls[0][-1].content)
    assert internal.count("event_id=") == 3
    assert internal.index(high.id) < internal.index(mid.id) < internal.index(low.id)
    assert "高优先级" in internal

    db_session.expire_all()
    assert {row.status for row in db_session.query(TurnEventInbox).all()} == {"injected"}


def test_injection_is_bounded_per_call(db_session) -> None:
    thread = _thread(db_session)
    turn = _running_turn(db_session, thread)
    for index in range(7):
        _enqueue(db_session, thread, key=f"evt-{index}", priority=50 + index)

    registry, tools = _registry(thread.id, turn)
    assistant = RecordingAssistant([AIMessage(content="[[EVENT_DECISION: continue]]完成")])
    _run_graph(db_session, thread, turn, assistant, registry, tools)

    db_session.expire_all()
    statuses = [row.status for row in db_session.query(TurnEventInbox).all()]
    assert statuses.count("injected") == 5
    assert statuses.count("pending") == 2


# ---------------------------------------------------------------------------
# 4. 主动消息：唯一时间线、顺序、幂等、原任务恢复、聊天不泄露内部提示
# ---------------------------------------------------------------------------


def test_notify_user_persists_single_message_and_resumes_task(db_session, monkeypatch) -> None:
    thread = _thread(db_session)
    turn = _running_turn(db_session, thread)
    thread_id = thread.id
    turn_id = turn.turn_id
    turn_record_id = turn.id
    execution_id = turn.execution_id or "exec-1"
    row = _enqueue(db_session, thread, key="evt-notify", priority=90, label="需要提醒")
    row_id = row.id

    registry, tools = _registry(thread_id, turn)
    assistant = RecordingAssistant([
        AIMessage(
            content="[[EVENT_DECISION: notify]]",
            tool_calls=[{
                "id": "call-notify-1",
                "name": NOTIFY_USER_TOOL_ID,
                "args": {"content": "资料已经整理到一半，需要你看一下。", "event_id": row_id},
            }],
        ),
        AIMessage(content="原任务完成"),
    ])
    result = _run_graph(db_session, thread, turn, assistant, registry, tools)

    assert len(assistant.calls) == 2, "notify 后必须回到原任务继续执行"
    assert result["event_decisions"][0]["decision"] == "notify"

    db_session.expire_all()
    proactive = [
        event for event in db_session.query(Event).filter(
            Event.thread_id == thread_id,
            Event.turn_id == turn_id,
            Event.event_type == "llm_response",
        ).all()
        if (event.payload or {}).get("proactive")
    ]
    assert len(proactive) == 1
    assert proactive[0].payload["content"] == "资料已经整理到一半，需要你看一下。"
    proactive_index = proactive[0].turn_event_index
    assert proactive_index == 1

    # 重复调用同一事件：不得重复写消息。
    db_session.commit()
    duplicate = _notify_user(
        content="资料已经整理到一半，需要你看一下。",
        event_id=row_id,
        ctx=RunContext(
            thread_id=thread_id, trace_id="trace-inject",
            turn_id=turn_id, turn_record_id=turn_record_id,
        ),
    )
    assert duplicate["created"] is False
    db_session.expire_all()
    assert db_session.query(Event).filter(
        Event.thread_id == thread_id,
        Event.turn_id == turn_id,
        Event.event_type == "llm_response",
    ).count() == 1

    stored = db_session.get(TurnEventInbox, row_id)
    assert stored is not None and stored.status == "handled"
    assert stored.decision == "notify"

    # finalize 把最终答复接在主动消息之后，并收口事件状态。
    db_session.expire_all()
    turn = db_session.get(TurnRecord, turn_record_id)
    assert turn is not None
    service = TurnExecutionService(llm_client=FakeLLMClient())
    ag_result = AgentGraphResult(
        reply="原任务完成", trace_id="trace-inject", user_message="原任务：整理资料",
        event_decisions=result["event_decisions"],
        memory_signal=SimpleNamespace(action="skip"),
    )
    heartbeat = TurnHeartbeat(turn_record_id, execution_id)
    service._finalize_turn(  # pyright: ignore[reportPrivateUsage]
        turn, execution_id, ag_result, heartbeat, assembled_ctx=None,
    )

    db_session.expire_all()
    events = db_session.query(Event).filter(
        Event.thread_id == thread_id,
        Event.turn_id == turn_id,
    ).order_by(Event.turn_event_index.asc()).all()
    final_event = [e for e in events if e.event_type == "llm_response" and not (e.payload or {}).get("proactive")][-1]
    assert proactive_index < final_event.turn_event_index

    page = ThreadState(db_session).list_thread_messages_page(thread_id)
    contents = [message["content"] for message in page["messages"]]
    assert "资料已经整理到一半，需要你看一下。" in contents
    assert contents.index("资料已经整理到一半，需要你看一下。") < contents.index("原任务完成")
    assert contents.count("资料已经整理到一半，需要你看一下。") == 1
    assert not any(INTERNAL_CONTEXT_HEADER in content for content in contents)
    assert not any("EVENT_DECISION" in content for content in contents)


# ---------------------------------------------------------------------------
# 5. 重复事件与 Outbox 兜底
# ---------------------------------------------------------------------------


def test_duplicate_event_is_deduped(db_session) -> None:
    thread = _thread(db_session)
    _running_turn(db_session, thread)
    first = _enqueue(db_session, thread, key="evt-dup")
    second = enqueue_turn_event(
        db_session,
        thread_id=thread.id,
        event_type="goal_trigger_fired",
        source="goal_trigger",
        dedupe_key="evt-dup",
        payload={"reason": "duplicate"},
    )
    db_session.commit()
    assert first.id
    assert second is None
    assert db_session.query(TurnEventInbox).count() == 1


def _plan(steps: list[dict]) -> dict:
    return {
        "summary": "事件注入测试",
        "timezone": "Asia/Shanghai",
        "acceptance": ["完成"],
        "steps": steps,
    }


def _confirmed_goal(db, thread: Thread) -> AgentTask:
    task = TaskRepository(db).create_task(
        thread_id=thread.id,
        goal="事件注入测试",
        title="事件注入测试",
        task_type="long_term_goal",
        task_brief={"scope": {"allowed_capabilities": ["get_current_time"]}},
        enqueue=False,
    )
    GoalService(db).attach_plan(task, _plan([
        {
            "id": "s1",
            "title": "等待事件",
            "kind": "autonomous",
            "acceptance": ["完成"],
            "trigger": {"kind": "event_condition", "event_types": ["task_completed"]},
        },
    ]))
    GoalService(db).confirm(task.id)
    db.commit()
    return task


def _decision_job(task: AgentTask, step_id: str, key: str) -> ClaimedJob:
    return ClaimedJob(
        id=str(uuid.uuid4()),
        job_type="goal_trigger_decision",
        payload={
            "task_id": task.id,
            "step_id": step_id,
            "trigger_key": key,
            "reason": "事件到达",
            "fired_at": datetime.now(timezone.utc).isoformat(),
            "pending_event_dedupe_key": "goal_trigger:op-guard",
        },
        trace_id="trace-guard",
        retry_count=0,
        max_retries=3,
        claim_token=str(uuid.uuid4()),
        schema_version=1,
        worker_id="test",
    )


def test_handler_defers_while_turn_active_then_recovers(db_session, monkeypatch) -> None:
    thread = _thread(db_session)
    turn = _running_turn(db_session, thread)
    task = _confirmed_goal(db_session, thread)
    plan = GoalService(db_session).load_plan(task)
    assert plan is not None
    step = plan.steps[0]
    step.last_trigger_key = "key-1"
    GoalService(db_session).save_plan(task, plan)
    row = _enqueue(db_session, thread, key="goal_trigger:op-guard", priority=90)
    row_id = row.id
    db_session.commit()

    calls: list[str] = []

    class FailingDecision:
        def __init__(self, *args, **kwargs):
            pass

        def decide(self, messages, trace_id=""):
            calls.append(trace_id)
            raise AssertionError("前台 Turn 活跃时不得发起第二次 LLM 决策")

    monkeypatch.setattr("aiive.worker.handlers_goal_triggers.GoalDecisionEngine", FailingDecision)
    job = _decision_job(task, step.id, "key-1")
    deferred = handle_goal_trigger_decision(job)
    assert deferred.outcome == HandlerOutcome.RETRY_LATER
    assert calls == []

    # Turn 结束（模拟正常完成或进程重启后租约失效）：Outbox 兜底继续决策。
    db_session.query(TurnRecord).filter(TurnRecord.id == turn.id).update({
        TurnRecord.status: "completed",
        TurnRecord.lease_expires_at: None,
    })
    db_session.commit()

    class NoActionDecision:
        def __init__(self, *args, **kwargs):
            pass

        def context(self, task, plan, step, trigger):
            return {}

        def messages(self, context):
            return [{"role": "system", "content": "no_action"}]

        def decide(self, messages, trace_id=""):
            calls.append(trace_id)
            from aiive.task_runtime.goal_decision import GoalDecision

            return GoalDecision(action="no_action"), {"model": "fake", "raw": "{}"}

        def apply(self, **kwargs):
            return {"status": "applied", "action": "no_action"}

        def write_trace(self, **kwargs):
            return None

    monkeypatch.setattr("aiive.worker.handlers_goal_triggers.GoalDecisionEngine", NoActionDecision)
    completed = handle_goal_trigger_decision(job)
    assert completed.outcome == HandlerOutcome.COMPLETED
    assert calls == ["trace-guard"]
    db_session.expire_all()
    stored = db_session.get(TurnEventInbox, row_id)
    assert stored is not None and stored.status == "expired"


def test_handler_skips_decision_when_turn_already_handled(db_session, monkeypatch) -> None:
    thread = _thread(db_session)
    _running_turn(db_session, thread)
    task = _confirmed_goal(db_session, thread)
    plan = GoalService(db_session).load_plan(task)
    assert plan is not None
    step = plan.steps[0]
    step.last_trigger_key = "key-2"
    GoalService(db_session).save_plan(task, plan)
    row = _enqueue(db_session, thread, key="goal_trigger:op-guard", priority=90)
    row.status = "handled"
    row.decision = "notify"
    db_session.commit()

    class FailingDecision:
        def __init__(self, *args, **kwargs):
            pass

        def decide(self, messages, trace_id=""):
            raise AssertionError("已在 Turn 内处理的事件不得二次决策")

    monkeypatch.setattr("aiive.worker.handlers_goal_triggers.GoalDecisionEngine", FailingDecision)
    result = handle_goal_trigger_decision(_decision_job(task, step.id, "key-2"))
    assert result.outcome == HandlerOutcome.COMPLETED
    assert "handled_in_turn" in result.reason
