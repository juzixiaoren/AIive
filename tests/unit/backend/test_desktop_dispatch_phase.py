"""Desktop 派发的发送边界：请求未发出与已发出必须给出不同结果。"""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from aiive.desktop.connection_manager import (
    DesktopConnectionManager,
    DesktopDispatchError,
    desktop_connection_manager,
)
from aiive.desktop.protocol import DesktopDispatchPhase
from aiive.desktop.registry_overlay import DesktopTool
from aiive.tools.base import ToolExecutionContext
from aiive.tools.executor import ToolExecutor
from aiive.tools.registry import CapabilitySafetySchema, ToolDefinition

NODE_ID = "node-phase-test"


@pytest.fixture
def loop_thread():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


class FakeSocket:
    """只实现 send_text 的 WebSocket 替身，用来精确控制发送边界。"""

    def __init__(self, *, fail: bool = False, on_send=None):
        self.fail = fail
        self.on_send = on_send
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("socket broken")
        payload = json.loads(text)
        self.sent.append(payload)
        if self.on_send is not None:
            self.on_send(payload)


def _manager(loop) -> DesktopConnectionManager:
    manager = DesktopConnectionManager()
    manager.set_main_loop(loop)
    return manager


def _dispatch(manager: DesktopConnectionManager, **overrides):
    kwargs = {
        "node_id": NODE_ID,
        "task_id": "task",
        "run_id": "run",
        "action_id": "action-1",
        "idempotency_key": "task:1:action:1",
        "arguments_hash": "a" * 64,
        "capability_id": "desktop_fs_write_text",
        "params": {"path": "/tmp/x"},
        "preconditions": {},
        "scope": {},
        "fencing_token": "fence",
        "timeout_seconds": 1,
    }
    kwargs.update(overrides)
    return manager.dispatch_action_sync(**kwargs)


def test_missing_main_loop_is_not_submitted():
    manager = DesktopConnectionManager()

    with pytest.raises(DesktopDispatchError) as raised:
        _dispatch(manager)

    assert raised.value.phase is DesktopDispatchPhase.NOT_SUBMITTED
    assert "main_loop_unavailable" in str(raised.value)


def test_offline_node_is_not_submitted(loop_thread):
    manager = _manager(loop_thread)

    with pytest.raises(DesktopDispatchError) as raised:
        _dispatch(manager)

    assert raised.value.phase is DesktopDispatchPhase.NOT_SUBMITTED
    assert str(raised.value) == "desktop_node_offline"


def test_invalid_envelope_never_reaches_the_socket(loop_thread):
    manager = _manager(loop_thread)
    socket = FakeSocket()
    manager.attach(NODE_ID, socket)

    with pytest.raises(DesktopDispatchError) as raised:
        # timeout_seconds 下限为 1：本地信封校验失败，请求一定没有发出。
        _dispatch(manager, timeout_seconds=-5)

    assert raised.value.phase is DesktopDispatchPhase.NOT_SUBMITTED
    assert "desktop_envelope_invalid" in str(raised.value)
    assert socket.sent == []


def test_send_failure_is_unknown_not_deterministic(loop_thread):
    manager = _manager(loop_thread)
    manager.attach(NODE_ID, FakeSocket(fail=True))

    with pytest.raises(DesktopDispatchError) as raised:
        _dispatch(manager)

    assert raised.value.phase is DesktopDispatchPhase.SUBMITTED
    assert "desktop_send_failed" in str(raised.value)


def test_wait_timeout_after_send_is_unknown(loop_thread):
    manager = _manager(loop_thread)
    socket = FakeSocket()
    manager.attach(NODE_ID, socket)

    with pytest.raises(DesktopDispatchError) as raised:
        _dispatch(manager, timeout_seconds=1)

    assert raised.value.phase is DesktopDispatchPhase.SUBMITTED
    assert str(raised.value) == "desktop_operation_timeout"
    # 请求确实发出去了：不能因为本地没等到 ACK 就认定没有执行。
    assert len(socket.sent) == 1
    assert socket.sent[0]["data"]["action_id"] == "action-1"


def test_node_terminal_result_is_returned_as_receipt(loop_thread):
    manager = _manager(loop_thread)
    resolved: list[dict] = []

    def on_send(payload: dict) -> None:
        data = payload["data"]
        resolved.append(data)
        manager.resolve_result(
            data["request_id"], node_id=NODE_ID, action_id=data["action_id"],
            ok=True, result={"content": "done"},
        )

    manager.attach(NODE_ID, FakeSocket(on_send=on_send))

    result = _dispatch(manager)

    assert result["ok"] is True
    assert result["result"] == {"content": "done"}
    assert result["action_id"] == "action-1"
    assert resolved[0]["fencing_token"] == "fence"


def test_node_pre_execution_rejection_is_terminal(loop_thread):
    manager = _manager(loop_thread)

    def on_send(payload: dict) -> None:
        data = payload["data"]
        manager.resolve_result(
            data["request_id"], node_id=NODE_ID, action_id=data["action_id"],
            ok=False, error="cancelled_before_start",
        )

    manager.attach(NODE_ID, FakeSocket(on_send=on_send))

    result = _dispatch(manager)

    assert result["ok"] is False
    assert result["error_type"] == "cancelled"
    assert result["error"] == "cancelled_before_start"


def _desktop_tool(capability_id: str = "desktop_fs_write_text") -> DesktopTool:
    return DesktopTool(
        ToolDefinition(
            safety=CapabilitySafetySchema(
                capability_id, "local_builtin", "trusted", "medium",
                writes_external_world=True,
                effect_mode="non_repeatable_external",
            ),
            description="desktop probe",
            parameters={},
            executor_kind="desktop_node",
            executor_node_id=NODE_ID,
        )
    )


def _task_action_context(authorization: dict) -> ToolExecutionContext:
    return ToolExecutionContext(
        "desktop_fs_write_text", channel="task_action", authorization=authorization,
    )


def _authorization() -> dict:
    return {
        "node_id": NODE_ID, "task_id": "task", "run_id": "run", "action_id": "action-1",
        "idempotency_key": "task:1:action:1", "arguments_hash": "a" * 64,
        "capability_id": "desktop_fs_write_text", "params": {}, "preconditions": {},
        "scope": {}, "fencing_token": "fence", "timeout_seconds": 5,
    }


def test_adapter_maps_not_submitted_to_deterministic_failure(monkeypatch):
    monkeypatch.setattr(
        desktop_connection_manager,
        "dispatch_action_sync",
        lambda **_kwargs: (_ for _ in ()).throw(
            DesktopDispatchError("desktop_node_offline", DesktopDispatchPhase.NOT_SUBMITTED)
        ),
    )

    payload = ToolExecutor.execute_direct(
        _desktop_tool(), _task_action_context(_authorization()), {}, 5,
    )

    assert payload["ok"] is False
    assert payload["error_type"] == "execution_failed"
    assert payload["outcome_known"] is True
    assert payload["side_effect_started"] is False
    assert payload["details"]["dispatch_phase"] == "not_submitted"


def test_adapter_maps_submitted_to_unknown(monkeypatch):
    monkeypatch.setattr(
        desktop_connection_manager,
        "dispatch_action_sync",
        lambda **_kwargs: (_ for _ in ()).throw(
            DesktopDispatchError("desktop_operation_timeout", DesktopDispatchPhase.SUBMITTED)
        ),
    )

    payload = ToolExecutor.execute_direct(
        _desktop_tool(), _task_action_context(_authorization()), {}, 5,
    )

    assert payload["ok"] is False
    assert payload["error_type"] == "execution_unknown"
    assert payload["outcome_known"] is False
    assert payload["side_effect_started"] is True


def test_chat_path_keeps_phase_semantics(monkeypatch):
    """非 Task 路径（DesktopTool._run → dispatch_sync）同样按阶段判定。"""
    monkeypatch.setattr(
        desktop_connection_manager,
        "dispatch_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DesktopDispatchError("desktop_node_offline", DesktopDispatchPhase.NOT_SUBMITTED)
        ),
    )

    payload = ToolExecutor.execute_direct(_desktop_tool(), ToolExecutionContext("desktop_fs_write_text"), {}, 5)

    assert payload["ok"] is False
    assert payload["error_type"] == "execution_failed"
    assert payload["outcome_known"] is True


def test_closed_loop_before_submit_leaves_no_pending():
    """初始检查通过但协程提交前循环关闭：请求没发出，不能留下 pending。"""
    manager = DesktopConnectionManager()
    closed = asyncio.new_event_loop()
    closed.close()
    manager.set_main_loop(closed)
    manager.attach(NODE_ID, FakeSocket())

    with pytest.raises(DesktopDispatchError) as raised:
        _dispatch(manager)

    assert raised.value.phase is DesktopDispatchPhase.NOT_SUBMITTED
    assert "main_loop_unavailable" in str(raised.value)
    assert manager._pending == {}


def test_dispatch_sync_closed_loop_leaves_no_pending():
    """v1 派发路径同样不能遗留 pending。"""
    manager = DesktopConnectionManager()
    closed = asyncio.new_event_loop()
    closed.close()
    manager.set_main_loop(closed)
    manager.attach(NODE_ID, FakeSocket())

    with pytest.raises(DesktopDispatchError) as raised:
        manager.dispatch_sync(NODE_ID, "desktop_fs_stat", {"path": "/tmp/x"})

    assert raised.value.phase is DesktopDispatchPhase.NOT_SUBMITTED
    assert manager._pending == {}


def test_submitted_failure_cleans_pending_but_stays_unknown(loop_thread):
    manager = _manager(loop_thread)
    manager.attach(NODE_ID, FakeSocket(fail=True))

    with pytest.raises(DesktopDispatchError) as raised:
        _dispatch(manager)

    # 清理 pending 只是回收等待器，不改变"请求已发出、结果未知"的结论。
    assert raised.value.phase is DesktopDispatchPhase.SUBMITTED
    assert "desktop_send_failed" in str(raised.value)
    assert manager._pending == {}


def test_timeout_cleanup_allows_retry_with_same_action_id(loop_thread):
    manager = _manager(loop_thread)
    socket = FakeSocket()
    manager.attach(NODE_ID, socket)

    with pytest.raises(DesktopDispatchError) as raised:
        _dispatch(manager, timeout_seconds=1)

    assert raised.value.phase is DesktopDispatchPhase.SUBMITTED
    assert manager._pending == {}

    def on_send(payload: dict) -> None:
        data = payload["data"]
        manager.resolve_result(
            data["request_id"], node_id=NODE_ID, action_id=data["action_id"],
            ok=True, result={"content": "done"},
        )

    socket.on_send = on_send
    result = _dispatch(manager)

    assert result["ok"] is True
    assert result["result"] == {"content": "done"}
    assert manager._pending == {}


def test_not_submitted_failure_allows_retry_with_same_action_id(loop_thread):
    manager = DesktopConnectionManager()
    socket = FakeSocket()
    manager.attach(NODE_ID, socket)
    closed = asyncio.new_event_loop()
    closed.close()
    manager.set_main_loop(closed)

    with pytest.raises(DesktopDispatchError) as raised:
        _dispatch(manager)
    assert raised.value.phase is DesktopDispatchPhase.NOT_SUBMITTED
    assert manager._pending == {}

    def on_send(payload: dict) -> None:
        data = payload["data"]
        manager.resolve_result(
            data["request_id"], node_id=NODE_ID, action_id=data["action_id"],
            ok=True, result={"content": "done"},
        )

    socket.on_send = on_send
    manager.set_main_loop(loop_thread)

    # 同一个 action_id 可以安全重派：没有遗留 pending 造成 already_in_flight。
    result = _dispatch(manager)

    assert result["ok"] is True
    assert manager._pending == {}


def test_stale_cleanup_never_removes_newer_pending():
    from aiive.desktop.connection_manager import _PendingDispatch

    manager = DesktopConnectionManager()
    old = _PendingDispatch(node_id=NODE_ID, action_id="action-1")
    newer = _PendingDispatch(node_id=NODE_ID, action_id="action-1")
    manager._pending["action-1"] = newer

    manager._discard_pending("action-1", old)

    assert manager._pending["action-1"] is newer
    manager._discard_pending("action-1", newer)
    assert "action-1" not in manager._pending
