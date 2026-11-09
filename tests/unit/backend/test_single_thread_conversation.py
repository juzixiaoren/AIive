"""唯一长期对话 + 三端互斥执行/远程中断验收测试（全离线）。

覆盖：
1. ConversationState 单例初始化与确定性主 Thread 选择；
2. 前台忙时立即失败，不创建 TurnRecord 也不写 user_message；
3. 拿锁成功后才落库；正常路径占用归属正确；
4. 取消收口：丢弃用户消息、保留工具事实、原子释放占用、不产生记忆抽取；
5. 取消的 Turn 不出现在历史消息；执行状态回到 idle；
6. 中断接口拒绝过期 turn_record_id，且请求到达时不提前释放锁。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from aiive.api.routes_conversation import InterruptRequest, interrupt_turn
from aiive.core.llm_client import FakeLLMClient
from aiive.db.models import (
    ConversationState,
    Event,
    OutboxJob,
    Thread,
    TurnRecord,
)
from aiive.runtime.agent_graph import ToolRecord
from aiive.runtime.conversation_state import (
    CONVERSATION_STATE_ID,
    describe_execution_status,
    get_current_thread_id,
)
from aiive.runtime.thread_lease import (
    OWNER_GOAL_DECISION,
    OWNER_TURN,
    acquire_thread_lease,
    lease_holder,
    release_thread_lease,
)
from aiive.runtime.thread_state import ThreadState
from aiive.runtime.turn_execution import TurnConflictError, TurnExecutionService


def _thread(db: Session, title: str = "main") -> str:
    thread = Thread(id=str(uuid.uuid4()), title=title)
    db.add(thread)
    db.commit()
    return thread.id


def _current_thread(db: Session) -> str:
    """取服务器唯一当前 Thread（必要时初始化）。"""
    return get_current_thread_id(db)


def _service() -> TurnExecutionService:
    return TurnExecutionService(llm_client=FakeLLMClient(), message_source="user")


def _user_events(db: Session, thread_id: str) -> list[Event]:
    return db.query(Event).filter(
        Event.thread_id == thread_id,
        Event.event_type == "user_message",
    ).all()


# ----------------------------------------------------------------------
# 1. 唯一当前对话
# ----------------------------------------------------------------------
def test_current_thread_is_singleton_and_prefers_existing_main_thread(db_session) -> None:
    quiet = _thread(db_session, "quiet")
    busy = _thread(db_session, "busy")
    for index in range(3):
        db_session.add(TurnRecord(
            id=str(uuid.uuid4()), thread_id=busy, turn_id=str(uuid.uuid4()),
            turn_sequence=index + 1, status="completed",
        ))
    db_session.commit()

    first = get_current_thread_id(db_session)
    second = get_current_thread_id(db_session)

    # 选择确定性：Turn 最多的既有线程，且多次读取稳定不变。
    assert first == second == busy
    state = db_session.get(ConversationState, CONVERSATION_STATE_ID)
    assert state is not None and state.current_thread_id == busy
    # 其他旧线程保留，不合并、不删除。
    assert db_session.get(Thread, quiet) is not None


def test_current_thread_created_only_when_library_is_empty(db_session) -> None:
    first = get_current_thread_id(db_session)
    second = get_current_thread_id(db_session)
    assert first == second
    assert db_session.get(Thread, first) is not None


# ----------------------------------------------------------------------
# 2. 忙状态：立即失败且不落库
# ----------------------------------------------------------------------
def test_busy_foreground_fails_without_persisting_anything(db_session) -> None:
    thread_id = _current_thread(db_session)
    held = acquire_thread_lease(
        thread_id,
        owner_kind=OWNER_GOAL_DECISION,
        owner_id="decision-1",
        ttl_seconds=60,
    )
    assert held is not None

    service = _service()
    turn_id = str(uuid.uuid4())
    with pytest.raises(TurnConflictError) as blocked:
        service._resolve_and_preempt("忙时提交", thread_id, turn_id)  # pyright: ignore[reportPrivateUsage]

    assert str(blocked.value) == "turn_in_progress"
    # 不创建 TurnRecord、不写 user_message（消息不排队、不落库）。
    assert db_session.query(TurnRecord).filter(TurnRecord.thread_id == thread_id).count() == 0
    assert _user_events(db_session, thread_id) == []
    holder = lease_holder(db_session, thread_id)
    assert holder is not None and holder["owner_kind"] == OWNER_GOAL_DECISION


# ----------------------------------------------------------------------
# 3. 取得占用后才落库
# ----------------------------------------------------------------------
def test_foreground_persists_only_after_lease_acquired(db_session) -> None:
    thread_id = _current_thread(db_session)
    service = _service()
    turn_id = str(uuid.uuid4())

    turn, _execution_id, token = service._resolve_and_preempt(  # pyright: ignore[reportPrivateUsage]
        "先拿锁再落库", thread_id, turn_id,
    )

    assert turn.status == "running"
    assert token >= 1
    stored = db_session.query(TurnRecord).filter(
        TurnRecord.thread_id == thread_id,
        TurnRecord.turn_id == turn_id,
    ).one()
    assert stored.status == "running"
    events = _user_events(db_session, thread_id)
    assert [event.payload["content"] for event in events] == ["先拿锁再落库"]
    holder = lease_holder(db_session, thread_id)
    assert holder is not None
    assert holder["owner_kind"] == OWNER_TURN and holder["owner_id"] == stored.id
    status = describe_execution_status(db_session, thread_id)
    assert status["state"] == "running"
    assert status["cancellable"] is True
    assert status["active_turn"]["turn_record_id"] == stored.id  # type: ignore[index]


# ----------------------------------------------------------------------
# 4. 取消收口
# ----------------------------------------------------------------------
def _cancel_turn(db_session, service, thread_id, message="取消我"):
    turn, execution_id, _token = service._resolve_and_preempt(  # pyright: ignore[reportPrivateUsage]
        message, thread_id, str(uuid.uuid4()),
    )
    record = ToolRecord(
        tool_call_id="call-1", name="run_shell", params={"cmd": "echo hi"},
        result={"ok": True, "result": "hi"}, status="completed",
    )
    assert service._finalize_cancelled_turn(  # pyright: ignore[reportPrivateUsage]
        turn, execution_id, "trace-cancel", [record],
    ) is True
    return turn


def test_cancelled_turn_drops_message_keeps_tool_facts_and_releases_lease(db_session) -> None:
    thread_id = _current_thread(db_session)
    service = _service()
    turn = _cancel_turn(db_session, service, thread_id)

    stored = db_session.get(TurnRecord, turn.id)
    assert stored is not None and stored.status == "cancelled"
    assert stored.lease_expires_at is None

    # 用户消息正文被清除（行保留供审计）。
    events = _user_events(db_session, thread_id)
    assert len(events) == 1
    assert events[0].payload["content"] == ""
    assert events[0].payload["redacted"] is True

    # 中断前真实副作用凭证保留。
    tool_events = db_session.query(Event).filter(
        Event.thread_id == thread_id,
        Event.event_type == "tool_result",
    ).all()
    assert len(tool_events) == 1
    assert tool_events[0].payload["name"] == "run_shell"
    assert tool_events[0].payload["cancelled"] is True

    # 不产生记忆抽取任务；占用已释放。
    assert db_session.query(OutboxJob).filter(
        OutboxJob.job_type == "memory_extraction",
        OutboxJob.payload["source_turn_record_id"].as_string() == turn.id,
    ).count() == 0
    assert lease_holder(db_session, thread_id) is None
    assert describe_execution_status(db_session, thread_id)["state"] == "idle"


def test_cancel_revokes_pending_conflict_reply_jobs(db_session) -> None:
    thread_id = _current_thread(db_session)
    service = _service()
    turn, execution_id, _token = service._resolve_and_preempt(  # pyright: ignore[reportPrivateUsage]
        "取消我", thread_id, str(uuid.uuid4()),
    )
    db_session.add(OutboxJob(
        operation_id=f"conflict_reply:case-1:{turn.turn_id}",
        job_type="memory_conflict_reply",
        status="pending",
        payload={"turn_id": turn.turn_id, "thread_id": thread_id},
    ))
    db_session.commit()

    assert service._finalize_cancelled_turn(  # pyright: ignore[reportPrivateUsage]
        turn, execution_id, "trace-cancel", [],
    ) is True

    job = db_session.query(OutboxJob).filter(
        OutboxJob.operation_id == f"conflict_reply:case-1:{turn.turn_id}",
    ).one()
    assert job.status == "cancelled"
    assert job.terminal_reason == "turn_cancelled"


def test_cancelled_turn_is_hidden_from_history_api(db_session) -> None:
    thread_id = _current_thread(db_session)
    service = _service()
    _cancel_turn(db_session, service, thread_id, message="这段输入必须消失")

    page = ThreadState(db_session).list_thread_messages_page(thread_id)
    user_messages = [item for item in page["messages"] if item["role"] == "user"]
    assert user_messages == []
    assert all("这段输入必须消失" not in item["content"] for item in page["messages"])


def test_cancelled_turn_not_injected_into_llm_history(db_session) -> None:
    from aiive.runtime.token_counter import LiteLLMTokenCounter
    from aiive.runtime.token_models import ModelProfile, TokenSafetyConfig

    thread_id = _current_thread(db_session)
    service = _service()
    _cancel_turn(db_session, service, thread_id, message="不要进入上下文")

    counter = LiteLLMTokenCounter(TokenSafetyConfig(), ModelProfile.from_config("deepseek", "fake-model"))
    messages, _stats = ThreadState(db_session).load_recent_messages_bounded(
        db_session, thread_id, token_budget=10000, token_counter=counter,
        model="fake-model",
    )
    assert all("不要进入上下文" not in str(message.get("content", "")) for message in messages)


# ----------------------------------------------------------------------
# 5. 中断接口
# ----------------------------------------------------------------------
def test_interrupt_rejects_stale_turn_and_keeps_lease(db_session) -> None:
    thread_id = _current_thread(db_session)
    service = _service()
    turn, _execution_id, _token = service._resolve_and_preempt(  # pyright: ignore[reportPrivateUsage]
        "正在回复", thread_id, str(uuid.uuid4()),
    )

    with pytest.raises(HTTPException) as stale:
        interrupt_turn(
            InterruptRequest(thread_id=thread_id, turn_record_id="old-turn-record"),
            db_session,
        )
    assert stale.value.status_code == 409
    assert stale.value.detail["code"] == "stale_turn"

    result = interrupt_turn(
        InterruptRequest(thread_id=thread_id, turn_record_id=turn.id),
        db_session,
    )
    assert result["status"] == "cancelling"
    stored = db_session.get(TurnRecord, turn.id)
    assert stored is not None and stored.cancel_requested_at is not None
    # 请求到达时不提前释放锁：必须等 Agent 真正停止后由执行者收口。
    holder = lease_holder(db_session, thread_id)
    assert holder is not None and holder["owner_id"] == turn.id


def test_interrupt_is_idempotent_and_never_releases_background_lease(db_session) -> None:
    thread_id = _current_thread(db_session)
    held = acquire_thread_lease(
        thread_id, owner_kind=OWNER_GOAL_DECISION, owner_id="decision-2", ttl_seconds=60,
    )
    assert held is not None
    with pytest.raises(HTTPException) as not_cancellable:
        interrupt_turn(InterruptRequest(thread_id=thread_id), db_session)
    assert not_cancellable.value.status_code == 409
    assert not_cancellable.value.detail["code"] == "not_cancellable"
    assert lease_holder(db_session, thread_id) is not None


def test_interrupt_after_completion_reports_idle(db_session) -> None:
    thread_id = _current_thread(db_session)
    service = _service()
    turn, _execution_id, _token = service._resolve_and_preempt(  # pyright: ignore[reportPrivateUsage]
        "已完成", thread_id, str(uuid.uuid4()),
    )
    # 模拟正常完成：completed + 占用已释放。
    db_session.query(TurnRecord).filter(TurnRecord.id == turn.id).update({
        TurnRecord.status: "completed",
        TurnRecord.lease_expires_at: None,
        TurnRecord.updated_at: datetime.now(UTC),
    }, synchronize_session=False)
    release_thread_lease(
        db_session, thread_id, owner_kind=OWNER_TURN, owner_id=turn.id,
    )
    db_session.commit()

    result = interrupt_turn(
        InterruptRequest(thread_id=thread_id, turn_record_id=turn.id), db_session,
    )
    assert result["status"] == "completed"
    stored = db_session.get(TurnRecord, turn.id)
    assert stored is not None and stored.cancel_requested_at is None


def test_stale_interrupt_does_not_cancel_newer_running_turn(db_session) -> None:
    thread_id = _current_thread(db_session)
    service = _service()
    finished, _execution_id, _token = service._resolve_and_preempt(  # pyright: ignore[reportPrivateUsage]
        "已完成的旧轮次", thread_id, str(uuid.uuid4()),
    )
    db_session.query(TurnRecord).filter(TurnRecord.id == finished.id).update({
        TurnRecord.status: "completed",
        TurnRecord.lease_expires_at: None,
        TurnRecord.updated_at: datetime.now(UTC),
    }, synchronize_session=False)
    release_thread_lease(
        db_session, thread_id, owner_kind=OWNER_TURN, owner_id=finished.id,
    )
    db_session.commit()

    running, _exec2, _token2 = service._resolve_and_preempt(  # pyright: ignore[reportPrivateUsage]
        "新的前台轮次", thread_id, str(uuid.uuid4()),
    )

    # 旧停止请求只拿到旧 Turn 的 completed 回执，不会误取消新的运行中 Turn。
    result = interrupt_turn(
        InterruptRequest(thread_id=thread_id, turn_record_id=finished.id), db_session,
    )
    assert result["status"] == "completed"
    current = db_session.get(TurnRecord, running.id)
    assert current is not None and current.status == "running"
    assert current.cancel_requested_at is None
    holder = lease_holder(db_session, thread_id)
    assert holder is not None and holder["owner_id"] == running.id


# ----------------------------------------------------------------------
# 6. 流式链路感知取消并停止后续输出
# ----------------------------------------------------------------------
class _StreamStub:
    """可控的 LangGraph 事件流：持续吐 token，记录是否被显式关闭。"""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks
        self.closed = False

    def astream_events(self, state, version="v2"):  # noqa: ANN001
        del state, version
        from langchain_core.messages import AIMessage

        async def _generate():
            try:
                for text in self._chunks:
                    yield {
                        "event": "on_chat_model_stream",
                        "name": "ChatOpenAI",
                        "run_id": "run-1",
                        "metadata": {"langgraph_node": "assistant"},
                        "data": {"chunk": AIMessage(content=text)},
                    }
            finally:
                # 只有事件流被真正关闭（aclose / 取消传播）才会执行到这里。
                self.closed = True

        return _generate()


def test_stream_stops_and_closes_graph_when_cancel_requested(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace
    from typing import Any, cast

    from langchain_core.messages import HumanMessage

    from aiive.memory.extraction_policy import MessageSource
    from aiive.runtime.agent_graph import AgentGraph, TurnCancelled
    from aiive.runtime.execution_context import TurnExecutionContext

    stub = _StreamStub(["第一段", "第二段", "第三段"])
    graph = AgentGraph(FakeLLMClient(), cast(Any, None))

    monkeypatch.setattr(
        graph, "_thread_state",
        SimpleNamespace(get_or_create_thread=lambda thread_id: SimpleNamespace(id=thread_id)),
    )
    monkeypatch.setattr(
        graph, "_prepare_execution",
        lambda *args, **kwargs: {
            "compiled": stub,
            "context_snapshot": None,
            "_records_raw": [],
            "pending_approvals": [],
            "initial_messages": [HumanMessage(content="原任务")],
        },
    )
    context = TurnExecutionContext(
        message_source=MessageSource.USER,
        thread_id="thread-cancel",
        turn_id="turn-cancel",
        turn_record_id="record-cancel",
        execution_id="exec-cancel",
        trace_id="trace-cancel",
    )

    async def _collect() -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        async for event in graph._execute_graph_stream(  # pyright: ignore[reportPrivateUsage]
            "原任务", context, cancel_check=lambda: True,
        ):
            events.append(event)
        return events

    with pytest.raises(TurnCancelled) as cancelled:
        asyncio.run(_collect())

    assert cancelled.value.trace_id == "trace-cancel"
    # 取消必须显式关闭事件流，把停止传播到 LangGraph 内部。
    assert stub.closed is True


# ----------------------------------------------------------------------
# 7. 正常完成释放占用，下一端可立即发送
# ----------------------------------------------------------------------
def test_completed_turn_releases_lease_for_next_sender(db_session) -> None:
    from types import SimpleNamespace

    from aiive.runtime.agent_graph import AgentGraphResult

    thread_id = _current_thread(db_session)
    service = _service()
    turn, execution_id, _token = service._resolve_and_preempt(  # pyright: ignore[reportPrivateUsage]
        "第一轮", thread_id, str(uuid.uuid4()),
    )

    ag_result = AgentGraphResult(
        reply="第一轮回复",
        trace_id="trace-complete",
        user_message="第一轮",
        memory_signal=SimpleNamespace(action="skip"),
    )
    response = service._finalize_turn(  # pyright: ignore[reportPrivateUsage]
        turn, execution_id, ag_result, SimpleNamespace(lease_lost=False),
    )
    assert response["reply"] == "第一轮回复"
    assert lease_holder(db_session, thread_id) is None
    assert describe_execution_status(db_session, thread_id)["state"] == "idle"

    # 下一端可以立即发送（无需等待租约过期）。
    second, _exec2, _token2 = service._resolve_and_preempt(  # pyright: ignore[reportPrivateUsage]
        "第二轮", thread_id, str(uuid.uuid4()),
    )
    assert second.status == "running"
    holder = lease_holder(db_session, thread_id)
    assert holder is not None and holder["owner_id"] == second.id


# ----------------------------------------------------------------------
# 8. 三端一致 + 跨端中断（HTTP 层）
# ----------------------------------------------------------------------
@pytest.fixture
def api_client(db_session):
    from fastapi.testclient import TestClient

    from aiive.db.base import get_db
    from aiive.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_three_clients_resolve_same_thread_id(api_client) -> None:
    """Web / Android / Desktop 三个客户端启动读取到同一个 Thread ID。"""
    ids = [
        api_client.get("/api/conversation/current").json()["thread_id"]
        for _ in range(3)
    ]
    assert len(set(ids)) == 1
    assert ids[0]

    status = api_client.get("/api/conversation/status").json()
    assert status["thread_id"] == ids[0]
    assert status["state"] == "idle"
    assert status["cancellable"] is False


def test_other_client_sees_running_and_can_interrupt(api_client, db_session) -> None:
    """A 端开始执行 → B 端看到 running/可中断 → B 端停止 → 收口后回到 idle。"""
    thread_id = api_client.get("/api/conversation/current").json()["thread_id"]
    service = _service()
    turn, execution_id, _token = service._resolve_and_preempt(  # pyright: ignore[reportPrivateUsage]
        "跨端中断", thread_id, str(uuid.uuid4()),
    )

    # B 端视角：当前有前台 Turn，按钮应切换为停止。
    status = api_client.get(f"/api/conversation/status?thread_id={thread_id}").json()
    assert status["state"] == "running"
    assert status["cancellable"] is True
    assert status["active_turn"]["turn_record_id"] == turn.id  # type: ignore[index]

    response = api_client.post("/api/conversation/interrupt", json={
        "thread_id": thread_id, "turn_record_id": turn.id,
    })
    assert response.status_code == 200
    assert response.json()["status"] == "cancelling"

    # 取消请求已登记但尚未收口：状态仍是 running，interrupting=True。
    interrupting = api_client.get(f"/api/conversation/status?thread_id={thread_id}").json()
    assert interrupting["state"] == "running"
    assert interrupting["interrupting"] is True
    assert interrupting["cancellable"] is True

    # 执行者真正停止后才收口：标记 cancelled、释放锁、广播 idle。
    assert service._finalize_cancelled_turn(  # pyright: ignore[reportPrivateUsage]
        turn, execution_id, "trace-http-cancel", [],
    ) is True
    assert api_client.get(f"/api/conversation/status?thread_id={thread_id}").json()["state"] == "idle"

    # 取消的用户消息不出现在历史 API。
    messages = api_client.get(f"/api/threads/{thread_id}/messages").json()["messages"]
    assert all(item.get("role") != "user" for item in messages)


def test_busy_chat_request_is_rejected_without_saving_message(
    api_client, db_session, monkeypatch,
) -> None:
    """Agent 输出/后台占用期间，另一端的请求被拒绝且不保存用户消息。"""
    from aiive.api.routes_chat import ChatRequest, chat as chat_route

    thread_id = api_client.get("/api/conversation/current").json()["thread_id"]
    held = acquire_thread_lease(
        thread_id,
        owner_kind=OWNER_GOAL_DECISION,
        owner_id="decision-busy",
        ttl_seconds=60,
    )
    assert held is not None

    monkeypatch.setattr(
        "aiive.api.routes_chat.default_llm_client", lambda: FakeLLMClient(),
    )
    with pytest.raises(HTTPException) as rejected:
        chat_route(ChatRequest(message="忙时不应保存", thread_id=thread_id))
    assert rejected.value.status_code == 202
    assert rejected.value.detail["code"] == "turn_in_progress"
    assert db_session.query(Event).filter(
        Event.thread_id == thread_id,
        Event.event_type == "user_message",
    ).count() == 0


# ----------------------------------------------------------------------
# 9. 强制唯一 Thread（V63 闭环）
# ----------------------------------------------------------------------
def test_user_message_with_stale_thread_id_is_rejected_before_any_write(db_session) -> None:
    current = _current_thread(db_session)
    stale = _thread(db_session, "old-thread")
    service = _service()
    turn_id = str(uuid.uuid4())

    with pytest.raises(TurnConflictError) as rejected:
        service._resolve_and_preempt("旧线程消息", stale, turn_id)  # pyright: ignore[reportPrivateUsage]

    assert str(rejected.value) == "stale_thread_id"
    assert db_session.query(TurnRecord).filter(TurnRecord.thread_id == stale).count() == 0
    assert _user_events(db_session, stale) == []
    assert lease_holder(db_session, stale) is None
    assert db_session.query(TurnRecord).filter(TurnRecord.thread_id == current).count() == 0


def test_user_message_without_thread_id_uses_current_thread(db_session) -> None:
    current = _current_thread(db_session)
    service = _service()
    turn, _execution_id, _token = service._resolve_and_preempt(  # pyright: ignore[reportPrivateUsage]
        "没传 thread_id", None, str(uuid.uuid4()),
    )
    assert turn.thread_id == current


@pytest.mark.parametrize("source", ["system_command", "runtime_event"])
def test_internal_sources_keep_their_own_thread_behavior(db_session, source: str) -> None:
    """内部来源不受唯一 Thread 约束（系统线程/提醒线程保持原行为）。"""
    _current_thread(db_session)
    internal_thread = _thread(db_session, "internal-thread")
    service = TurnExecutionService(llm_client=FakeLLMClient(), message_source=source)

    turn, _execution_id, _token = service._resolve_and_preempt(  # pyright: ignore[reportPrivateUsage]
        "内部消息", internal_thread, str(uuid.uuid4()),
    )
    assert turn.status == "running"
    assert turn.thread_id == internal_thread


def test_stream_busy_payload_has_202_retryable_contract(db_session) -> None:
    import asyncio

    from aiive.api.routes_chat import _chat_error_payload

    thread_id = _current_thread(db_session)
    held = acquire_thread_lease(
        thread_id,
        owner_kind=OWNER_GOAL_DECISION,
        owner_id="decision-stream-busy",
        ttl_seconds=60,
    )
    assert held is not None

    service = _service()

    async def _first_event() -> dict | None:
        async for event in service.execute_turn_stream(
            "流式忙时请求", thread_id, str(uuid.uuid4()),
        ):
            return event
        return None

    event = asyncio.run(_first_event())
    assert event is not None
    assert event["type"] == "error"
    assert event["error"] == "turn_in_progress"
    # SSE 的 HTTP 状态保持 200，但 error 事件内的 status 必须正确。
    assert event["_status"] == 202
    assert event["retryable"] is True

    payload = _chat_error_payload(event)
    assert payload["code"] == "turn_in_progress"
    assert payload["status"] == 202
    assert payload["retryable"] is True

def test_background_lease_reports_busy_and_not_cancellable(db_session) -> None:
    thread_id = _current_thread(db_session)
    held = acquire_thread_lease(
        thread_id,
        owner_kind=OWNER_GOAL_DECISION,
        owner_id="decision-busy-status",
        ttl_seconds=60,
    )
    assert held is not None

    status = describe_execution_status(db_session, thread_id)
    assert status["state"] == "busy"
    assert status["cancellable"] is False
    assert status["active_turn"] is not None
