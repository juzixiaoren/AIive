"""
Tool Registry模块

工具注册表，所有操作都注册为工具。
Agent 通过调用工具来完成任务。
"""

from typing import Any, Callable


class Tool:
    """工具定义"""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        func: Callable[..., Any]
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.func = func

    def to_schema(self) -> dict[str, Any]:
        """转换为 LLM 可理解的工具 schema"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }

    def execute(self, **kwargs) -> dict[str, Any]:
        """执行工具"""
        try:
            result = self.func(**kwargs)
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "error": str(e)}


class ToolRegistry:
    """工具注册表"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        func: Callable[..., Any]
    ) -> None:
        """注册工具"""
        self._tools[name] = Tool(name, description, parameters, func)

    def get_tool(self, name: str) -> Tool | None:
        """获取工具"""
        return self._tools.get(name)

    def get_all_tools(self) -> list[Tool]:
        """获取所有工具"""
        return list(self._tools.values())

    def get_tools_schema(self) -> list[dict[str, Any]]:
        """获取所有工具的 schema，用于 LLM"""
        return [tool.to_schema() for tool in self._tools.values()]

    def execute_tool(self, name: str, **kwargs) -> dict[str, Any]:
        """执行工具"""
        tool = self.get_tool(name)
        if not tool:
            return {"success": False, "error": f"工具不存在: {name}"}
        return tool.execute(**kwargs)


# 全局工具注册表
_tool_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    """获取全局工具注册表"""
    global _tool_registry
    if _tool_registry is None:
        _tool_registry = ToolRegistry()
    return _tool_registry
