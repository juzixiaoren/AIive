"""工具审批安全链路回归测试。"""
import hashlib
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from aiive.api import routes_approval
from aiive.api.routes_approval import ApprovalRespondRequest
from aiive.db.models import ApprovalRequest, Event, Thread, TurnRecord
from aiive.runtime.policy_engine import PolicyAction, check_tool_calls
from aiive.runtime.working_state import WorkingStateService
from aiive.tools.registry import CapabilitySafetySchema, ToolRegistration, ToolRegistry


def _seed_approval(db, approval_id: str = "approval-1") -> ApprovalRequest:
    """创建绑定固定 Turn 和服务端参数的待审批记录。"""
    thread = Thread(id="thread-1")
    turn = TurnRecord(
        id="turn-record-1",
        thread_id=thread.id,
        turn_id="turn-1",
        turn_sequence=1,
        status="completed",
        request_fingerprint="fingerprint",
    )
    db.add(thread)
    db.flush()
    db.add(turn)
    db.flush()
    approval = ApprovalRequest(
        id=approval_id,
        thread_id=thread.id,
        turn_record_id=turn.id,
        turn_id=turn.turn_id,
        trace_id="trace-1",
        tool_call_id="call-1",
        tool_name="dangerous_tool",
        tool_args={"value": "server-value"},
        tool_args_hash=hashlib.sha256(json.dumps(
            {"value": "server-value"}, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest(),
        descriptor_hash="descriptor-1",
        risk_snapshot={"risk_level": "high"},
        status="pending",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(approval)
    db.flush()
    WorkingStateService().add_pending_approval(
        db, thread.id, approval.id, "confirm:dangerous_tool",
    )
    db.commit()
    return approval


def _registry(calls: list[dict[str, str]]) -> ToolRegistry:
    """构造需要确认且记录真实参数的测试工具注册表。"""
    registry = ToolRegistry()

    def handler(value: str) -> dict[str, str]:
        calls.append({"value": value})
        return {"value": value}

    registry.register(ToolRegistration(
        safety=CapabilitySafetySchema(
            capability_id="dangerous_tool",
            definition_source="local_builtin",
            definition_trust_level="trusted",
            risk_level="high",
            requires_confirmation=True,
            allowed_instruction_sources=["trusted_user_command"],
            descriptor_hash="descriptor-1",
        ),
        handler=handler,
    ))
    return registry


def test_registered_tool_requires_confirmation() -> None:
    """显式标记的高风险工具必须进入审批。"""
    registry = _registry([])

    result = check_tool_calls([{"name": "dangerous_tool", "args": {}, "id": "call-1"}], registry)

    assert result.action is PolicyAction.CONFIRM
    assert result.allowed_tools == ["dangerous_tool"]
    assert result.confirm_tools == ["dangerous_tool"]


def test_unknown_tool_remains_blocked() -> None:
    """审批启用后仍不得绕过未注册工具边界。"""
    result = check_tool_calls([{"name": "unknown_tool", "args": {}, "id": "call-1"}], ToolRegistry())

    assert result.action is PolicyAction.BLOCK
    assert result.blocked_tools == ["unknown_tool"]


def test_request_rejects_client_tool_payload() -> None:
    """客户端不得提交工具、参数或线程来替换服务端审批内容。"""
    with pytest.raises(ValidationError):
        ApprovalRespondRequest.model_validate({
            "approval_id": "approval-1",
            "action": "approve",
            "tool_name": "forged_tool",
            "tool_args": {"value": "forged"},
            "thread_id": "forged-thread",
        })


def test_approval_uses_server_payload_and_is_idempotent(db, monkeypatch) -> None:
    """批准只执行服务端冻结参数，重复请求返回已保存结果。"""
    _seed_approval(db)
    calls: list[dict[str, str]] = []
    registry = _registry(calls)
    monkeypatch.setattr(routes_approval, "get_tool_registry", lambda: registry)

    first = routes_approval.respond_approval(ApprovalRespondRequest(
        approval_id="approval-1", action="approve",
    ))
    second = routes_approval.respond_approval(ApprovalRespondRequest(
        approval_id="approval-1", action="approve",
    ))

    assert first["ok"] is True
    assert first["action"] == "succeeded"
    assert second["idempotent_replay"] is True
    assert calls == [{"value": "server-value"}]

    db.expire_all()
    approval = db.get(ApprovalRequest, "approval-1")
    assert approval is not None and approval.status == "succeeded"
    events = db.query(Event).filter(Event.turn_id == "turn-1").order_by(Event.turn_event_index).all()
    assert [event.event_type for event in events] == ["tool_call", "tool_result"]
    assert events[0].payload["params"] == {"value": "server-value"}


def test_tampered_server_snapshot_never_executes(db, monkeypatch) -> None:
    """服务端参数快照哈希不一致时失败关闭且不得执行。"""
    approval = _seed_approval(db)
    approval.tool_args = {"value": "tampered"}
    db.commit()
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(routes_approval, "get_tool_registry", lambda: _registry(calls))

    with pytest.raises(routes_approval.HTTPException) as error:
        routes_approval.respond_approval(ApprovalRespondRequest(
            approval_id="approval-1", action="approve",
        ))

    assert error.value.status_code == 409
    assert calls == []
    db.expire_all()
    stored = db.get(ApprovalRequest, "approval-1")
    assert stored is not None and stored.status == "failed"


def test_missing_approval_never_executes(db, monkeypatch) -> None:
    """不存在的审批必须失败关闭，不能进入工具执行。"""
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(routes_approval, "get_tool_registry", lambda: _registry(calls))

    with pytest.raises(routes_approval.HTTPException) as error:
        routes_approval.respond_approval(ApprovalRespondRequest(
            approval_id="missing", action="approve",
        ))

    assert error.value.status_code == 404
    assert calls == []


def test_empty_descriptor_hash_never_executes() -> None:
    """空审批定义指纹必须失败关闭，不得调用工具处理器。"""
    calls: list[dict[str, str]] = []
    registry = _registry(calls)

    result = registry.execute_approved(
        "dangerous_tool",
        {"value": "server-value"},
        expected_descriptor_hash="",
        run_context=routes_approval.RunContext(
            thread_id="thread-1", trace_id="trace-1", source="approval",
        ),
        tool_call_id="call-1",
    )

    assert result["ok"] is False
    assert result["error_type"] == "missing_descriptor_hash"
    assert calls == []


def test_descriptor_change_invalidates_approval(db, monkeypatch) -> None:
    """审批后工具定义变化时不得执行旧授权。"""
    _seed_approval(db)
    calls: list[dict[str, str]] = []
    registry = _registry(calls)
    registered = registry.get("dangerous_tool")
    assert registered is not None
    registry.register(ToolRegistration(
        safety=CapabilitySafetySchema(
            capability_id="dangerous_tool",
            definition_source="local_builtin",
            definition_trust_level="trusted",
            risk_level="high",
            requires_confirmation=True,
            allowed_instruction_sources=["trusted_user_command"],
            descriptor_hash="descriptor-2",
        ),
        handler=registered.handler,
    ))
    monkeypatch.setattr(routes_approval, "get_tool_registry", lambda: registry)

    response = routes_approval.respond_approval(ApprovalRespondRequest(
        approval_id="approval-1", action="approve",
    ))

    assert response["ok"] is False
    assert response["action"] == "failed"
    assert calls == []
