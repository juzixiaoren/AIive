from __future__ import annotations

import uuid

import pytest

from aiive.control.capability_broker import CapabilityBroker
from aiive.control.scope import TaskScope
from aiive.control.task_policy import TaskPolicyEngine
from aiive.control.tool_trust import ToolIdentity, ToolTrustPolicy
from aiive.db.models import AgentTask, Capability, DesktopNode
from aiive.desktop.protocol import DESKTOP_CAPABILITIES
from aiive.desktop.registry_overlay import register_desktop_tools
from aiive.mcp.bootstrap import register_capability_tools
from aiive.runtime.policy_engine import PolicyAction, check_tool_calls
from aiive.tools.registry import ToolRegistry, get_tool_registry


def _desktop(registry: ToolRegistry, node_id: str) -> None:
    node = DesktopNode(id=node_id, name="test node", capabilities=[
        {"name": name, "description": name} for name in DESKTOP_CAPABILITIES
    ])
    register_desktop_tools(registry, node)


def _mcp(registry: ToolRegistry, executor: str = "installation-1", description: str = "File editor") -> None:
    register_capability_tools(registry, "server-filesystem", "mcp:filesystem", [
        {"name": "read_file"}, {"name": "write_file", "description": description},
        {"name": "delete_file"}, {"name": "edit_file"}, {"name": "rename_file"},
    ], execution_identity=executor)


@pytest.fixture
def registry():
    tools = get_tool_registry().snapshot()
    _desktop(tools, "test-node-1")
    _mcp(tools)
    return tools


@pytest.mark.parametrize(("name", "arguments", "approval"), [
    *[(name, {}, True) for name in (
        "safe_delete", "edit_file", "write_file", "apply_patch", "apply_patch_to_inactive_slot",
        "run_selfdev_repair", "forget", "install_mcp_sandbox", "promote_slot", "rollback_slot",
        "desktop_fs_write_text", "desktop_fs_write_binary", "desktop_fs_edit_text", "desktop_fs_mkdir",
        "desktop_fs_copy", "desktop_fs_move", "desktop_fs_delete",
        "mcp_server_filesystem_write_file", "mcp_server_filesystem_delete_file",
        "mcp_server_filesystem_edit_file", "mcp_server_filesystem_rename_file",
    )],
    ("desktop_exec", {"command": "ls", "cwd": "/work"}, True),
    ("run_shell", {"command": "ls", "cwd": "/work"}, False),
    ("run_shell", {"command": "echo hi > file", "cwd": "/work"}, True),
    ("run_shell", {"command": "rm file", "cwd": "/work"}, True),
    ("run_shell", {"command": "unknown-cmd", "cwd": "/work"}, True),
    ("run_shell", {"command": 'cat "$PATH"', "cwd": "/work"}, True),
    ("shell_session", {"session_id": "test", "cancel": True}, False),
    *[(name, {}, False) for name in (
        "desktop_fs_read_text", "desktop_fs_stat", "mcp_server_filesystem_read_file", "read_file",
        "remember_or_update", "memory_search", "schedule_reminder", "cancel_reminder",
        "web_search", "fetch_web_page", "ingest_document", "reindex_document",
    )],
])
def test_standard_matrix_shared_by_main_and_worker(registry, tmp_path, name, arguments, approval):
    tool = registry.get(name)
    assert tool is not None
    identity = ToolIdentity.from_tool(tool)
    # Standard ignores the saved selection.
    standard = ToolTrustPolicy(approval_exempt_capabilities=frozenset({identity}))
    if "cwd" in arguments:
        arguments = {**arguments, "cwd": str(tmp_path)}
    scope = TaskScope(frozenset(), (str(tmp_path),), frozenset(), allow_network=True, capability_policy="unified")
    calls = [{"name": name, "args": arguments}]
    main = check_tool_calls(calls, registry, trust=standard)
    worker = TaskPolicyEngine().decide(tool, scope, name, arguments, trust=standard)
    assert main.action == (PolicyAction.CONFIRM if approval else PolicyAction.ALLOW)
    assert worker.outcome == ("approval" if approval else "allow")


def test_full_trust_exempts_every_registered_tool(registry):
    full = ToolTrustPolicy(mode="full_trust")
    assert all(full.approval_reason(tool, {}) is None for tool in registry.registrations())
    assert check_tool_calls([{"name": "missing"}], registry, trust=full).action is PolicyAction.BLOCK


def test_partial_trust_pins_exact_definition_source_and_executor(registry):
    selected = [registry.get(name) for name in (
        "run_shell", "desktop_fs_write_text", "mcp_server_filesystem_write_file",
    )]
    identities = frozenset(ToolIdentity.from_tool(tool) for tool in selected)
    partial = ToolTrustPolicy(mode="partial_trust", approval_exempt_capabilities=identities)
    assert all(partial.approval_reason(tool, {"command": "rm file"}) is None for tool in selected)
    assert partial.approval_reason(registry.get("forget"), {})

    modified = ToolRegistry()
    _desktop(modified, "test-node-2")
    _mcp(modified, executor="installation-2")
    desktop = modified.get("desktop_fs_write_text")
    mcp = modified.get("mcp_server_filesystem_write_file")
    assert mcp.safety.descriptor_hash == selected[2].safety.descriptor_hash
    assert partial.approval_reason(desktop, {})
    assert partial.approval_reason(mcp, {})
    _mcp(modified, description="Replacement contract")
    assert partial.approval_reason(modified.get("mcp_server_filesystem_write_file"), {})


def test_workspace_and_desktop_execution_guards_still_apply(db_session, tmp_path, registry):
    task = AgentTask(id=str(uuid.uuid4()), task_type="coding", task_brief={"scope": {
        "capability_policy": "unified", "allowed_roots": [str(tmp_path)],
    }}, task_state={"worktree_path": str(tmp_path)})
    broker = CapabilityBroker(db_session)
    full = ToolTrustPolicy(mode="full_trust")
    assert broker.prepare(task, "write_file", {"path": "new.txt", "content": "test"}, available=registry).decision.outcome == "approval"
    assert broker.prepare(task, "read_file", {"path": "new.txt"}, available=registry).decision.outcome == "allow"
    cases = [
        ("write_file", {"path": "../outside.txt", "content": "test"}, "path_outside_task_workspace"),
        ("write_file", {"path": "new.txt", "content": 1}, "invalid_tool_arguments"),
        ("run_shell", {"command": "cat ../outside.txt"}, "shell_path_outside_task_scope"),
        ("run_shell", {"command": "curl https://example.invalid"}, "network_access_not_in_task_scope"),
        ("desktop_exec", {"command": "ls", "cwd": str(tmp_path), "env": {"TOKEN": "x"}}, "secret_environment_not_allowed"),
    ]
    for name, arguments, reason in cases:
        decision = broker.prepare(task, name, arguments, available=registry, trust=full).decision
        assert decision.outcome == "block" and decision.reason.startswith(reason)
    db_session.add(Capability(name="filesystem", capability_id="mcp:filesystem", state="disabled"))
    db_session.flush()
    decision = broker.prepare(task, "mcp_server_filesystem_write_file", {}, available=registry, trust=full).decision
    assert (decision.outcome, decision.reason) == ("block", "capability_disabled")
