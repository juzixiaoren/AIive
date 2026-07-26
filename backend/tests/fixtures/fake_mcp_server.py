"""测试夹具：最小 MCP stdio server（Python 实现，避免测试依赖 node/npm）。

用官方 mcp SDK 的 FastMCP 起一个只有两个工具的 stdio server：
- echo(text): 返回 "echo:" + text
- boom(): 恒抛异常（验证 isError 传播）

由 test_mcp_pipeline.py 以 `python fake_mcp_server.py` 子进程方式启动。
"""
from mcp.server.fastmcp import FastMCP

server = FastMCP("fake-mcp-server")


@server.tool()
def echo(text: str) -> str:
    """Echo back the input text."""
    return "echo:" + text


@server.tool()
def boom() -> str:
    """Always fails (for error propagation tests)."""
    raise RuntimeError("boom tool always fails")


if __name__ == "__main__":
    server.run(transport="stdio")
