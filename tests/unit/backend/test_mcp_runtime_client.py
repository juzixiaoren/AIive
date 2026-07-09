"""测试 MCP 运行时客户端——工具注册、调用和错误处理。"""
from aiive.mcp.runtime_client import MCPRuntimeClient, MCPToolResult


class TestMCPRuntimeClient:
    """测试 MCPRuntimeClient 的工具管理功能。"""

    def test_register_and_call_tool(self):
        """验证注册工具后能正常调用。"""
        client = MCPRuntimeClient()

        def greet(name: str = "World") -> str:
            return f"Hello, {name}!"

        client.register_tool("greet", greet)
        result = client.call_tool("greet", {"name": "AIive"})
        assert result.ok is True
        assert result.result == "Hello, AIive!"

    def test_unknown_tool_returns_error(self):
        """验证调用未注册工具时返回错误。"""
        client = MCPRuntimeClient()
        result = client.call_tool("nonexistent", {})
        assert result.ok is False
        assert "Unknown tool" in result.error

    def test_tool_error_returns_error(self):
        """验证工具执行异常时正确返回错误信息。"""
        client = MCPRuntimeClient()

        def bad_tool():
            raise ValueError("something went wrong")

        client.register_tool("bad", bad_tool)
        result = client.call_tool("bad", {})
        assert result.ok is False
        assert "something went wrong" in result.error

    def test_list_tools(self):
        """验证列出已注册工具列表。"""
        client = MCPRuntimeClient()
        client.register_tool("t1", lambda: None)
        client.register_tool("t2", lambda: None)
        tools = client.list_tools()
        assert "t1" in tools
        assert "t2" in tools

    def test_tool_result_is_untrusted(self):
        """验证工具结果默认标记为不可信。"""
        client = MCPRuntimeClient()
        assert client.tool_result_is_untrusted() is True

    def test_result_is_mcp_tool_result(self):
        """验证调用结果类型为 MCPToolResult。"""
        client = MCPRuntimeClient()

        def echo(msg: str = "") -> str:
            return msg

        client.register_tool("echo", echo)
        result = client.call_tool("echo", {"msg": "hello"})
        assert isinstance(result, MCPToolResult)
