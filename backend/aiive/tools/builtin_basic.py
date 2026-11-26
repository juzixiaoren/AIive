"""Domain tool handlers (implementation) split from builtin_tools."""

from aiive.tools.base import BaseTool

from typing import override


class EchoTool(BaseTool):
    @override
    def _run(self, message: str = "") -> str:
        """回显工具：原样返回输入消息。"""
        return message
