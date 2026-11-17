"""本机 Bash 工具 handler：在 AIive 主机上执行真实 bash 命令。"""
from __future__ import annotations

from aiive.tools.base import BaseTool, ToolExecutionContext

from typing import Any

from aiive.tools.local_shell import run_bash


class RunShellTool(BaseTool):
    def _run(self, command: str, cwd: str, timeout_seconds: int = 0, _execution: ToolExecutionContext | None = None) -> dict[str, Any]:
        """在 AIive 所在主机用真实 bash 执行单条命令。

        参数:
            command: 要执行的命令文本（由 bash -c 解析）。
            cwd: 工作目录，必须位于任务允许的根目录内（由 TaskScope 校验）。
            timeout_seconds: 超时秒数；0 表示使用默认值，上限由配置约束。

        返回:
            结构化结果：ok / exit_code / stdout / stderr / timed_out / duration_ms /
            truncated / cwd / shell（path 与 version）。
        """
        return run_bash(command, cwd=cwd, timeout_seconds=timeout_seconds or None,
                        cancel_check=(lambda: _execution.cancellation.requested) if _execution else None)
