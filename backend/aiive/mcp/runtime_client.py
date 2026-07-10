"""
MCP 运行时客户端：模拟 MCP（Model Context Protocol）运行环境，用于测试。
将本地可调用对象包装为 MCP 工具，提供统一的 tool call 接口。
"""
import logging

from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class MCPToolResult:
    """MCP 工具调用结果。

    属性:
        ok: 调用是否成功
        result: 成功时的返回数据
        error: 失败时的错误信息
    """
    ok: bool
    result: Any = None
    error: str | None = None


class MCPRuntimeClient:
    """模拟的 MCP 运行时客户端，用于测试环境。

    将本地可调用对象包装为 MCP 工具，提供与真实 MCP 客户端一致的接口。
    """

    def __init__(self):
        """初始化运行时客户端，创建空的工具注册表。"""
        self._tools: dict[str, Callable[..., Any]] = {}

    def register_tool(self, name: str, handler: Callable[..., Any]) -> None:
        """注册一个本地工具到 MCP 运行时。

        参数:
            name: 工具名称
            handler: 工具处理函数
        """
        self._tools[name] = handler

    def call_tool(self, name: str, params: dict[str, Any]) -> MCPToolResult:
        """调用已注册的工具。

        参数:
            name: 工具名称
            params: 调用参数

        返回:
            MCPToolResult，包含执行结果或错误信息
        """
        handler = self._tools.get(name)
        if not handler:
            return MCPToolResult(ok=False, error=f"Unknown tool: {name}")

        try:
            result = handler(**params)
            return MCPToolResult(ok=True, result=result)
        except Exception as e:
            logger.warning("MCP工具调用失败: tool=%s params=%s error=%s", name, params, e)
            return MCPToolResult(ok=False, error=str(e))

    def list_tools(self) -> list[str]:
        """列出所有已注册的工具名称。

        返回:
            工具名称列表
        """
        return list(self._tools.keys())

    def tool_result_is_untrusted(self) -> bool:
        """检查工具结果是否来自不信任来源。
        MCP 输出始终被视为不信任内容。

        返回:
            始终为 True
        """
        return True  # 始终为 True：MCP 输出是不信任内容
