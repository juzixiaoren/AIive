from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class MCPToolResult:
    ok: bool
    result: Any = None
    error: str | None = None


class MCPRuntimeClient:
    """Simulated MCP runtime client for testing. Wraps local callables as MCP tools."""

    def __init__(self):
        self._tools: dict[str, Callable[..., Any]] = {}

    def register_tool(self, name: str, handler: Callable[..., Any]) -> None:
        self._tools[name] = handler

    def call_tool(self, name: str, params: dict[str, Any]) -> MCPToolResult:
        handler = self._tools.get(name)
        if not handler:
            return MCPToolResult(ok=False, error=f"Unknown tool: {name}")

        try:
            result = handler(**params)
            return MCPToolResult(ok=True, result=result)
        except Exception as e:
            return MCPToolResult(ok=False, error=str(e))

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def tool_result_is_untrusted(self) -> bool:
        return True  # Always: MCP output is untrusted content
