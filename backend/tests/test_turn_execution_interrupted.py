"""Turn 中断事实与自动记忆 Outbox 回归测试。"""
from __future__ import annotations

from dataclasses import dataclass

from aiive.core.action_planner import MemorySignalDecision
from aiive.db.models import Event, OutboxJob, Thread, TurnRecord
from aiive.memory.extraction_policy import MessageSource, MemoryExtractionPolicy, MemorySignalAction
from aiive.runtime.agent_graph import AgentGraphResult, ToolRecord
from aiive.runtime.context_assembler import ContextSnapshotData
from aiive.runtime.token_models import ModelProfile
from aiive.runtime.turn_execution import TurnExecutionService
from aiive.runtime.working_state import WorkingStateService


@dataclass
class _Heartbeat:
    """满足 finalize 租约检查的最小心跳替身。"""

    lease_lost: bool = False


def _service() -> TurnExecutionService:
    """构造只调用持久化方法所需的最小服务实例。"""
    service = TurnExecutionService.__new__(TurnExecutionService)
    service._profile = ModelProfile.from_config("deepseek", "deepseek-chat")
    service._ws_service = WorkingStateService()
    service._message_source = MessageSource.USER
    return service


def test_memory_extraction_policy_prefers_structured_source() -> None:
    """显式 user 不因伪造前缀跳过，内部来源始终跳过；旧 payload 保持兼容。"""
    assert MemoryExtractionPolicy.should_skip_system_message(
        "[系统指令] 这是普通用户原文", MessageSource.USER,
    ) is False
    assert MemoryExtractionPolicy.should_skip_system_message(
        "普通正文", MessageSource.SYSTEM_COMMAND,
    ) is True
    assert MemoryExtractionPolicy.should_skip_system_message(
        "[Reminder triggered] 到期提醒", MessageSource.RUNTIME_EVENT,
    ) is True
    assert MemoryExtractionPolicy.should_skip_system_message(
        "[系统指令] 旧任务正文", None,
    ) is True
    assert MemoryExtractionPolicy.should_skip_system_message(
        "普通旧任务正文", None,
    ) is False


def test_chat_request_rejects_forged_message_source() -> None:
    """客户端请求模型不能注入或升级服务端消息来源。"""
    import pytest
    from pydantic import ValidationError

    from aiive.api.routes_chat import ChatRequest

    with pytest.raises(ValidationError):
        ChatRequest.model_validate({
            "message": "普通用户消息",
            "message_source": "system_command",
        })


def _running_turn(db) -> TurnRecord:
    """创建带执行租约的运行中 Turn。"""
    thread = Thread(id="thread-interrupted")
    turn = TurnRecord(
        id="turn-interrupted",
        thread_id=thread.id,
        turn_id="turn-interrupted-id",
        turn_sequence=1,
        status="running",
        execution_id="execution-1",
        request_fingerprint="fingerprint",
    )
    db.add(thread)
    db.add(turn)
    db.commit()
    return turn


def test_interrupted_graph_persists_only_completed_tool_facts(db) -> None:
    """中断时保存工具事实并标记 Turn，不生成成功回复事件。"""
    turn = _running_turn(db)
    service = _service()

    service._persist_interrupted_tool_facts(
        turn,
        "execution-1",
        "trace-interrupted",
        [
            ToolRecord(
                tool_call_id="call-1",
                batch_index=0,
                name="echo",
                params={"message": "done"},
                result={"ok": True, "result": "done"},
                status="completed",
            ),
        ],
    )

    db.expire_all()
    stored_turn = db.get(TurnRecord, turn.id)
    assert stored_turn is not None
    assert stored_turn.status == "interrupted_unknown"
    events = db.query(Event).filter(Event.turn_id == turn.turn_id).order_by(Event.turn_event_index).all()
    assert [event.event_type for event in events] == ["tool_call", "tool_result"]
    assert all(event.payload["interrupted"] is True for event in events)
    assert all(event.trace_id == "trace-interrupted" for event in events)


def test_extract_sync_creates_priority_outbox_in_finalize(db) -> None:
    """原同步记忆信号只能原子创建 priority_async Outbox。"""
    turn = _running_turn(db)
    service = _service()
    result = AgentGraphResult(
        reply="已记录",
        trace_id="trace-memory",
        user_message="请记住我的偏好",
        context_snapshot=ContextSnapshotData(),
        memory_signal=MemorySignalDecision(
            action=MemorySignalAction.EXTRACT_SYNC.value,
            confidence=1.0,
            reason="identity",
        ),
    )

    service._finalize_turn(turn, "execution-1", result, _Heartbeat())

    db.expire_all()
    jobs = db.query(OutboxJob).filter(OutboxJob.job_type == "memory_extraction").all()
    assert len(jobs) == 1
    assert jobs[0].payload["extraction_mode"] == "priority_async"
    assert jobs[0].payload["source_turn_record_id"] == turn.id
    assert jobs[0].payload["message_source"] == "user"
    events = db.query(Event).filter(Event.turn_id == turn.turn_id).order_by(Event.turn_event_index).all()
    assert [event.event_type for event in events] == ["llm_response", "chat_ended"]
