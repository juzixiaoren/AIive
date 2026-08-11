"""Electron Desktop Node 在线租约与动态工具覆盖层测试。"""
from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from aiive.context.run_context import RunContext
from aiive.db.models import DesktopNode, Thread
from aiive.desktop.connection_manager import (
    DesktopConnectionManager,
    DesktopDispatchError,
    desktop_connection_manager,
)
from aiive.desktop.node_service import desktop_node_service
from aiive.desktop.protocol import DesktopJournalEntry
from aiive.desktop.registry_overlay import build_registry_for_thread
from aiive.desktop.schemas import DesktopHello
from aiive.runtime.policy_engine import PolicyAction, check_tool_calls


def _hello(node_id: str) -> DesktopHello:
    return DesktopHello.model_validate({
        "node_id": node_id,
        "name": "Windows PC",
        "platform": "win32",
        "arch": "x64",
        "app_version": "0.1.0",
        "capabilities": [
            {
                "name": "desktop_fs_stat",
                "description": "读取本机文件状态",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "desktop_fs_write_text",
                "description": "写入本机文本文件",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
                "risk_level": "medium",
                "writes_external_world": True,
            },
            {
                "name": "desktop_fs_delete",
                "description": "删除本机文件（模拟旧客户端错误地声明为低风险）",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        ],
    })


def test_register_heartbeat_and_explicit_binding(db_session) -> None:
    node_id = str(uuid.uuid4())
    thread = Thread()
    db_session.add(thread)
    db_session.flush()

    node = desktop_node_service.register_online(db_session, _hello(node_id))
    desktop_node_service.bind_thread(db_session, thread.id, node_id)
    db_session.flush()

    assert node.status == "online"
    assert node.lease_expires_at is not None
    assert desktop_node_service.heartbeat(db_session, node_id) is True
    assert desktop_node_service.resolve_online_node(db_session, thread.id).id == node_id

    desktop_node_service.mark_offline(db_session, node_id)
    db_session.flush()
    assert desktop_node_service.resolve_online_node(db_session, thread.id) is None


def test_single_online_node_is_implicitly_available(db_session) -> None:
    node_id = str(uuid.uuid4())
    thread = Thread()
    db_session.add(thread)
    db_session.flush()
    desktop_node_service.register_online(db_session, _hello(node_id))

    resolved = desktop_node_service.resolve_online_node(db_session, thread.id)

    assert resolved is not None and resolved.id == node_id


def test_registry_overlay_injects_node_tools_with_remote_executor(db_session, monkeypatch) -> None:
    node_id = str(uuid.uuid4())
    thread = Thread()
    db_session.add(thread)
    db_session.flush()
    desktop_node_service.register_online(db_session, _hello(node_id))
    db_session.flush()
    monkeypatch.setattr(desktop_connection_manager, "is_connected", lambda candidate: candidate == node_id)
    monkeypatch.setattr(
        desktop_connection_manager,
        "dispatch_sync",
        lambda candidate, capability, params, timeout: {
            "node_id": candidate,
            "capability": capability,
            "path": params["path"],
        },
    )

    registry = build_registry_for_thread(db_session, thread.id)

    readonly = registry.get("desktop_fs_stat")
    writer = registry.get("desktop_fs_write_text")
    deleter = registry.get("desktop_fs_delete")
    assert readonly is not None
    assert writer is not None
    assert deleter is not None
    assert readonly.executor_kind == writer.executor_kind == "desktop_node"
    assert writer.safety.writes_external_world is True
    assert deleter.safety.risk_level == "high"
    assert deleter.safety.requires_confirmation is True
    assert deleter.safety.can_delete is True
    assert "读取本机文件状态" not in readonly.description
    assert "读取目标电脑文件或目录的元数据" in readonly.description
    assert readonly.parameters["path"]["required"] is True
    assert readonly.handler(path="C:\\Users\\me\\note.txt") == {
        "node_id": node_id,
        "capability": "desktop_fs_stat",
        "path": "C:\\Users\\me\\note.txt",
    }
    assert check_tool_calls([
        {"name": "desktop_fs_delete", "args": {"path": "C:\\Users\\me\\old.txt"}, "id": "delete-1"},
    ], registry).action is PolicyAction.CONFIRM
    # Desktop 能力不再接受 Turn 审批入口；只能由 Task CapabilityBroker 使用
    # execute_brokered 进入持久 Action 链。
    approved = registry.execute_brokered(
        "desktop_fs_delete",
        {"path": "C:\\Users\\me\\old.txt"},
        deleter.safety.descriptor_hash,
        RunContext(thread_id=thread.id, trace_id="trace-1", source="task_runtime", task_id="task-1"),
    )
    assert approved["ok"] is True
    assert approved["result"]["capability"] == "desktop_fs_delete"
    monkeypatch.setattr(
        desktop_connection_manager,
        "dispatch_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DesktopDispatchError("desktop_node_disconnected")
        ),
    )
    disconnected = registry.execute_brokered(
        "desktop_fs_stat",
        {"path": "C:\\Users\\me\\note.txt"},
        readonly.safety.descriptor_hash,
        RunContext(thread_id=thread.id, trace_id="trace-1", source="task_runtime", task_id="task-1"),
    )
    assert disconnected["error_type"] == "execution_unknown"


def test_offline_node_tools_are_not_injected(db_session, monkeypatch) -> None:
    node_id = str(uuid.uuid4())
    thread = Thread()
    db_session.add(thread)
    db_session.flush()
    desktop_node_service.register_online(db_session, _hello(node_id))
    db_session.flush()
    monkeypatch.setattr(desktop_connection_manager, "is_connected", lambda _candidate: False)

    registry = build_registry_for_thread(db_session, thread.id)

    assert registry.get("desktop_fs_stat") is None
    assert db_session.get(DesktopNode, node_id) is not None


def test_task_explicit_node_routes_without_ambiguous_thread_binding(db_session, monkeypatch) -> None:
    first_id = str(uuid.uuid4())
    second_id = str(uuid.uuid4())
    thread = Thread()
    db_session.add(thread)
    db_session.flush()
    desktop_node_service.register_online(db_session, _hello(first_id))
    desktop_node_service.register_online(db_session, _hello(second_id))
    db_session.flush()
    monkeypatch.setattr(desktop_connection_manager, "is_connected", lambda _candidate: True)

    registry = build_registry_for_thread(db_session, thread.id, second_id)

    registration = registry.get("desktop_fs_stat")
    assert registration is not None
    assert registration.executor_node_id == second_id


def test_stale_socket_detach_does_not_disconnect_replacement() -> None:
    manager = DesktopConnectionManager()
    old_socket = object()
    new_socket = object()
    manager.attach("node-1", old_socket)  # type: ignore[arg-type]
    manager.attach("node-1", new_socket)  # type: ignore[arg-type]

    assert manager.is_current_connection("node-1", old_socket) is False  # type: ignore[arg-type]
    assert manager.is_current_connection("node-1", new_socket) is True  # type: ignore[arg-type]
    assert manager.detach("node-1", old_socket) is False  # type: ignore[arg-type]
    assert manager.is_connected("node-1") is True
    assert manager.detach("node-1", new_socket) is True  # type: ignore[arg-type]
    assert manager.is_connected("node-1") is False


def test_committed_journal_entry_requires_result_hash() -> None:
    with pytest.raises(ValidationError, match="requires SHA-256 result_hash"):
        DesktopJournalEntry(
            action_id="action-1",
            idempotency_key="task:1:action:1",
            arguments_hash="a" * 64,
            status="committed",
            result={"ok": True},
        )

    entry = DesktopJournalEntry(
        action_id="action-1",
        idempotency_key="task:1:action:1",
        arguments_hash="a" * 64,
        status="committed",
        result_hash="b" * 64,
        result={"ok": True},
    )
    assert entry.status == "committed"


def test_result_cannot_resolve_another_node_or_action() -> None:
    manager = DesktopConnectionManager()
    pending_type = type(manager._pending)  # noqa: SLF001 - focused protocol invariant test
    assert pending_type is dict

    from aiive.desktop.connection_manager import _PendingDispatch

    pending = _PendingDispatch(node_id="node-1", action_id="action-1")
    manager._pending["action-1"] = pending  # noqa: SLF001

    assert manager.resolve_result(
        "action-1", node_id="node-2", action_id="action-1", ok=True,
    ) is False
    assert manager.resolve_result(
        "action-1", node_id="node-1", action_id="action-2", ok=True,
    ) is False
    assert pending.event.is_set() is False
    assert manager.resolve_result(
        "action-1", node_id="node-1", action_id="action-1", ok=True, result={"ok": True},
    ) is True
    assert pending.event.is_set() is True
