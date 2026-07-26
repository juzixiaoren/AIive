"""测试 MCP 运行时客户端（真实 stdio 协议版本）的纯逻辑行为。

说明：旧的进程内 mock API（register_tool + 直接调 Python callable）已随
真实 MCP 链路移除。需要真实子进程的协议行为（initialize 握手、tools/list、
tools/call、isError 传播、会话复用）由
backend/tests/test_mcp_pipeline.py::TestRuntimeClient 用 Python 假 MCP
server 夹具（backend/tests/fixtures/fake_mcp_server.py）完整覆盖；
本文件只保留无需启动子进程的单元用例。
"""
from types import SimpleNamespace

import pytest

from aiive.mcp.runtime_client import (
    MCPLaunchSpec,
    MCPRuntimeClient,
    MCPToolResult,
    _convert_call_result,
    build_launch_spec,
    tool_result_is_untrusted,
)


class TestUntrustedContract:
    """MCP 输出恒为不信任内容的契约。"""

    def test_client_tool_result_is_untrusted(self):
        client = MCPRuntimeClient()
        assert client.tool_result_is_untrusted() is True

    def test_module_level_tool_result_is_untrusted(self):
        assert tool_result_is_untrusted() is True

    def test_mcp_tool_result_untrusted_by_default(self):
        assert MCPToolResult(ok=True, result={}).untrusted is True
        assert MCPToolResult(ok=False, error="x").untrusted is True


class TestConfiguration:
    """启动规格登记与未配置能力的失败行为。"""

    def test_configure_and_is_configured(self):
        client = MCPRuntimeClient()
        assert client.is_configured("mcp:x") is False
        client.configure("mcp:x", MCPLaunchSpec(command="node", args=("a.js",)))
        assert client.is_configured("mcp:x") is True

    def test_list_tools_unconfigured_raises(self):
        client = MCPRuntimeClient()
        with pytest.raises(RuntimeError, match="not configured"):
            client.list_tools("mcp:nowhere", timeout=1, startup_timeout=1)

    def test_call_tool_unconfigured_returns_honest_error(self):
        """call_tool 不抛异常，返回 ok=False + untrusted 标记。"""
        client = MCPRuntimeClient()
        result = client.call_tool("mcp:nowhere", "echo", {}, timeout=1, startup_timeout=1)
        assert isinstance(result, MCPToolResult)
        assert result.ok is False
        assert result.untrusted is True
        assert "not configured" in result.error


class TestBuildLaunchSpec:
    """启动命令只能由安装记录的结构化字段拼装，拒绝任意命令。"""

    def test_rejects_non_node_runner(self):
        with pytest.raises(ValueError, match="unsupported MCP runner"):
            build_launch_spec({"runner": "cmd.exe", "entry_js": "x.js"})

    def test_rejects_missing_entry_js_field(self):
        with pytest.raises(ValueError, match="missing entry_js"):
            build_launch_spec({"runner": "node"})

    def test_rejects_nonexistent_entry_file(self):
        with pytest.raises(ValueError, match="not found"):
            build_launch_spec({"runner": "node", "entry_js": "Z:/no/such/entry.js"})


class TestConvertCallResult:
    """SDK CallToolResult → MCPToolResult 的转换逻辑。"""

    def test_success_result_with_text_and_structured(self):
        raw = SimpleNamespace(
            content=[SimpleNamespace(text="hello")],
            structuredContent={"k": 1},
            isError=False,
        )
        result = _convert_call_result(raw)
        assert isinstance(result, MCPToolResult)
        assert result.ok is True
        assert result.untrusted is True
        assert result.result["content"] == ["hello"]
        assert result.result["structured"] == {"k": 1}

    def test_is_error_maps_to_failed_result(self):
        raw = SimpleNamespace(
            content=[SimpleNamespace(text="something went wrong")],
            structuredContent=None,
            isError=True,
        )
        result = _convert_call_result(raw)
        assert result.ok is False
        assert result.untrusted is True
        assert "something went wrong" in result.error

    def test_empty_error_content_gets_placeholder(self):
        raw = SimpleNamespace(content=[], structuredContent=None, isError=True)
        result = _convert_call_result(raw)
        assert result.ok is False
        assert result.error
