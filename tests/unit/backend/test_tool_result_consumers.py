"""工具结果契约的下游消费者：LangChain 内容、回执解析与 MCP 结果语义。"""
from __future__ import annotations

import json
import uuid

from aiive.context.run_context import RunContext
from aiive.mcp.bootstrap import MCPTool, MCP_TOOL_TIMEOUT_SECONDS
from aiive.mcp.runtime_client import (
    MCPRuntimeClient,
    MCPToolResult,
    _convert_call_result,
)
from aiive.runtime.tool_events import parse_tool_execution_receipt
from aiive.tools.base import BaseTool, ToolExecutionContext
from aiive.tools.executor import ToolExecutor
from aiive.tools.langchain_adapter import build_langchain_tools
from aiive.tools.registry import (
    CapabilitySafetySchema,
    ToolDefinition,
    get_tool_registry,
)


def _langchain_tool(registry, capability_id: str):
    run_context = RunContext(thread_id="thread", trace_id="trace")
    tools = build_langchain_tools(registry, run_context, [capability_id])
    assert len(tools) == 1
    return tools[0]


def test_langchain_tool_message_keeps_failure_and_business_fields(tmp_path) -> None:
    registry = get_tool_registry()
    target = tmp_path / "outside.txt"
    target.write_text("secret", encoding="utf-8")
    tool = _langchain_tool(registry, "read_text_file")

    content = tool.invoke({"path": str(target)})
    parsed = json.loads(content)

    assert parsed["ok"] is False
    assert parsed["error_type"] == "access_denied"
    assert parsed["outcome_known"] is True
    # 业务字段仍然平铺在内容里，模型不需要理解新的嵌套结构。
    assert parsed["path"] == str(target)
    receipt = parse_tool_execution_receipt(content)
    assert receipt.ok is False
    assert receipt.status == "failed"


def test_langchain_success_content_is_unchanged() -> None:
    registry = get_tool_registry()
    tool = _langchain_tool(registry, "echo")

    # 纯字符串结果保持原样，不被包装成 JSON 信封（模型侧协议不变）。
    assert tool.invoke({"message": "hello"}) == "hello"

    dict_tool = _langchain_tool(registry, "get_runtime_identity")
    parsed = json.loads(dict_tool.invoke({}))

    # 成功结果的业务字典仍直接作为内容，不额外包一层 ok 信封。
    assert "deployment_state" in parsed
    assert parse_tool_execution_receipt(json.dumps(parsed)).ok is True


def _mcp_tool(capability_id: str = "mcp:test_probe") -> MCPTool:
    safety = CapabilitySafetySchema(
        capability_id,
        "remote_mcp",
        "untrusted",
        "medium",
        writes_external_world=True,
        effect_mode="non_repeatable_external",
        timeout_seconds=MCP_TOOL_TIMEOUT_SECONDS,
    )
    return MCPTool(
        ToolDefinition(safety=safety, description="probe", parameters={}),
        "mcp:test_capability",
        "probe",
    )


def test_mcp_protocol_error_is_a_deterministic_failure(monkeypatch) -> None:
    class _Item:
        text = "server rejected the call"

    class _CallResult:
        content = [_Item()]
        structuredContent = None
        isError = True

    class _Client:
        def call_tool(self, *_args, **_kwargs):
            return _convert_call_result(_CallResult())

    monkeypatch.setattr("aiive.mcp.bootstrap.get_runtime_client", lambda: _Client())
    tool = _mcp_tool()

    payload = ToolExecutor.execute_direct(
        tool, ToolExecutionContext(tool.safety.capability_id), {}, 5,
    )

    assert payload["ok"] is False
    assert payload["error_type"] == "execution_failed"
    assert payload["outcome_known"] is True
    assert "server rejected" in payload["error"]


def test_mcp_transport_failure_keeps_unknown_outcome(monkeypatch) -> None:
    class _Client:
        def call_tool(self, *_args, **_kwargs):
            return MCPToolResult(
                ok=False, error="request timed out after submit", outcome_known=False,
            )

    monkeypatch.setattr("aiive.mcp.bootstrap.get_runtime_client", lambda: _Client())
    tool = _mcp_tool()

    payload = ToolExecutor.execute_direct(
        tool, ToolExecutionContext(tool.safety.capability_id), {}, 5,
    )

    assert payload["ok"] is False
    assert payload["error_type"] == "execution_unknown"
    assert payload["outcome_known"] is False


def test_mcp_client_distinguishes_startup_and_submitted_failures(monkeypatch) -> None:
    client = MCPRuntimeClient()

    def _raise_launch(*_args, **_kwargs):
        raise RuntimeError("cannot launch server")

    monkeypatch.setattr(client, "_ensure_worker", _raise_launch)
    startup = client.call_tool("mcp:probe", "probe", {})
    assert startup.ok is False
    assert startup.outcome_known is True

    monkeypatch.setattr(client, "_ensure_worker", lambda *_args, **_kwargs: object())

    def _raise_submit(*_args, **_kwargs):
        raise TimeoutError("request timeout")

    monkeypatch.setattr(client, "_submit_with_restart", _raise_submit)
    submitted = client.call_tool("mcp:probe", "probe", {})
    assert submitted.ok is False
    assert submitted.outcome_known is False

def test_desktop_disconnect_keeps_unknown_outcome(monkeypatch) -> None:
    from aiive.desktop.connection_manager import (
        classify_pending_error,
        desktop_connection_manager,
    )

    # Node 在 Action 执行期间断连：真实结果未知，不能降级为确定性失败。
    assert classify_pending_error("desktop_node_disconnected") == "execution_unknown"
    assert classify_pending_error("cancelled_before_start") == "cancelled"
    assert classify_pending_error("precondition_sha256_changed") == "precondition_failed"
    assert classify_pending_error("node_bug") == "execution_failed"

    monkeypatch.setattr(
        desktop_connection_manager,
        "dispatch_action_sync",
        lambda **_kwargs: {
            "ok": False,
            "error": "desktop_node_disconnected",
            "error_type": classify_pending_error("desktop_node_disconnected"),
        },
    )
    from aiive.desktop.registry_overlay import DesktopTool

    # executor_kind=desktop_node + channel=task_action 才会走节点适配器。
    desktop_tool = DesktopTool(
        ToolDefinition(
            safety=CapabilitySafetySchema(
                "desktop_fs_write_text", "local_builtin", "trusted", "medium",
                writes_external_world=True,
                effect_mode="non_repeatable_external",
            ),
            description="desktop probe",
            parameters={},
            executor_kind="desktop_node",
            executor_node_id="node-1",
        )
    )

    payload = ToolExecutor.execute_direct(
        desktop_tool,
        ToolExecutionContext(
            "desktop_fs_write_text", channel="task_action",
            authorization={"node_id": "node-1"},
        ),
        {},
        5,
    )

    assert payload["ok"] is False
    assert payload["error_type"] == "execution_unknown"
    assert payload["outcome_known"] is False
    assert payload["side_effect_started"] is True


class _PartialFailureProbe(BaseTool):
    def _run(self):
        return {
            "ok": False,
            "error_type": "execution_failed",
            "error": "写了一半文件",
            "outcome_known": True,
            "side_effect_started": True,
            "written": 1,
        }


def test_langchain_content_keeps_side_effect_flags() -> None:
    registry = get_tool_registry()
    capability_id = f"contract_partial_{uuid.uuid4().hex[:8]}"
    registry.register(
        _PartialFailureProbe(
            ToolDefinition(
                CapabilitySafetySchema(capability_id, "test", "trusted", "low"),
                description="partial failure probe",
                parameters={},
            )
        )
    )
    try:
        content = _langchain_tool(registry, capability_id).invoke({})
    finally:
        registry.unregister(capability_id)

    parsed = json.loads(content)
    assert parsed["ok"] is False
    assert parsed["error_type"] == "execution_failed"
    assert parsed["outcome_known"] is True
    assert parsed["side_effect_started"] is True
    assert parsed["written"] == 1
